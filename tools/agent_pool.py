#!/usr/bin/env python3
"""agent_pool.py — locked, dependency-aware scheduler for parallel coding agents.

This replaces the pre-assignment model (``tools/spawn_agents.sh``). Instead of an
orchestrator handing each agent a feature, an agent *self-claims* the best
unclaimed, dependency-ready feature under a lock, so several interactive Claude
sessions can run at once with no file/branch/port collisions.

State lives in the **primary checkout** (resolved via ``git worktree list``) so
every worktree's invocation shares one set of files + one lock:

* ``tools/feature_deps.json``   — committed DAG of feature dependencies
  (seeded + self-learned). Source of "what blocks what".
* ``tools/.agent_runtime.json`` — gitignored, ephemeral leases
  (``{"leases": {id: {owner, ts, expiry, port_index}}}``). ``owner`` is
  ``host:pid`` of the live session (see ``claim_and_work.sh``), used to avoid
  reclaiming a feature whose process is still alive.
* ``tools/.agent_pool.lock``    — gitignored ``fcntl.flock`` mutex (macOS lacks
  the ``flock`` binary, so all locking is done here in Python).

``passes`` truth is read from ``origin/main:feature_list.json`` (the integrated
state), falling back to the local working file when offline.

Subcommands: ``seed``, ``status``, ``claim``, ``block``, ``integrate``,
``heartbeat``, ``release``. See ``prompts/coding_prompt.md`` and AGENTS.md.
"""

from __future__ import annotations

import argparse
import difflib
import fcntl
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ----------------------------------------------------------------------------
# Tunables
# ----------------------------------------------------------------------------
LEASE_TTL = int(os.environ.get("ATP_LEASE_TTL", 2 * 3600))  # seconds
PORT_STRIDE = 10
DEV_BASE, IB_LIVE_BASE, IB_PAPER_BASE = 3000, 4001, 4002
INTEGRATE_MARKER = "[agent-integrate]"
# The ONLY paths `integrate` may stage into its marker commit. Staging is
# restricted to this allowlist (never `git add -A`) so a retry's `reset --hard`
# can never sweep in and then drop an agent's feature/test/progress work.
INTEGRATE_ALLOWLIST = ("feature_list.json", "progress.txt", "progress.d", "tools/feature_deps.json")

# Coarse category -> subsystem/crate map. Used only to *prefer* a feature whose
# crate no active sibling holds, to reduce merge conflicts. Unmapped categories
# fall back to the category name (still distinct, just less crate-accurate).
CATEGORY_SUBSYSTEM = {
    "data": "atp-data",
    "market_data": "atp-market-data",
    "execution": "atp-execution",
    "safety": "atp-execution",
    "error_handling": "atp-execution",
    "strategy_api": "atp-strategy-engine",
    "simulation": "atp-simulation",
    "backtesting": "atp-simulation",
    "factor_pipeline": "atp-factor-pipeline",
    "research": "atp-factor-pipeline",
    "orchestration": "atp-orchestrator",
    "reservoir": "atp-orchestrator",
    "notifications": "atp-notification",
    "dashboard": "dashboard",
    "ui_requirements": "dashboard",
    "interfaces": "dashboard",
    "logging": "atp-types",
    "reliability": "atp-execution",
    "performance": "atp-types",
    "security": "atp-types",
    "architecture": "atp-types",
}

# Substrings (case-insensitive) in a feature's description/steps that mean it is
# NOT solo-verifiable in a parallel session — integrate must use --mode
# serialized (not complete) unless the agent passes --force-complete. Heuristic
# backstop for the honesty guard; the agent's own classification still decides.
SERIALIZED_KEYWORDS = [
    "integration test",
    "interactive brokers",
    "ib gateway",
    " ib ",
    "live execution",
    "live order",
    "live trading",
    "live designation",
    "real-time market data",
    "playwright",
    "e2e",
    "websocket",
    "jupyter",
    "dashboard",
]

# Curated seed edges {feature: [prerequisites]}. Only edges whose endpoints both
# exist in feature_list.json are kept (filtered in `seed`). The graph self-learns
# the rest via `block`. Conservative on purpose — over-seeding serializes work.
SEED_DEPS = {
    # --- Data layer: storage substrate underpins ingestion + tiering ---
    "SRS-DATA-001": ["SRS-DATA-008", "SRS-DATA-013"],
    "SRS-DATA-002": ["SRS-DATA-008", "SRS-DATA-013"],
    "SRS-DATA-003": ["SRS-DATA-008", "SRS-DATA-001"],
    "SRS-DATA-004": ["SRS-DATA-008", "SRS-DATA-013"],
    "SRS-DATA-005": ["SRS-DATA-008", "SRS-DATA-013"],
    "SRS-DATA-006": ["SRS-DATA-008", "SRS-DATA-013"],
    "SRS-DATA-009": ["SRS-DATA-008"],
    "SRS-DATA-010": ["SRS-DATA-008", "SRS-DATA-017"],
    "SRS-DATA-017": ["SRS-DATA-008"],
    "SRS-DATA-018": ["SRS-DATA-008"],
    "SRS-DATA-014": ["SRS-DATA-013"],
    # --- Corporate actions: normalization + live/paper adjust sit on 011 ---
    "SRS-DATA-012": ["SRS-DATA-011"],
    "SRS-DATA-019": ["SRS-DATA-011"],
    "SRS-DATA-020": ["SRS-DATA-011"],
    "SRS-DATA-021": ["SRS-DATA-011"],
    # --- Market data: stale-blocking needs heartbeat freshness ---
    "SRS-MD-004": ["SRS-MD-003"],
    # --- Reservoir: ranking needs the paper pool; hot-swap ordering ---
    "SRS-RESV-002": ["SRS-RESV-001"],
    "SRS-RESV-004": ["SRS-RESV-003"],
    "SRS-RESV-005": ["SRS-RESV-003"],
    "SRS-RESV-006": ["SRS-RESV-003"],
    # --- SDK: non-standard bars build on time-based resampling ---
    "SRS-SDK-008": ["SRS-SDK-007"],
}


# ----------------------------------------------------------------------------
# Paths (resolved against the primary checkout)
# ----------------------------------------------------------------------------
def _run(cmd, *, cwd=None, check=True, capture=True):
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        capture_output=capture,
    )


def primary_root() -> Path:
    """The main worktree path (git lists it first), shared by all worktrees."""
    out = _run(["git", "worktree", "list", "--porcelain"]).stdout
    for line in out.splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree ") :]).resolve()
    raise SystemExit("✗ agent_pool: cannot determine primary worktree (not a git repo?)")


ROOT = primary_root()
FEATURE_FILE = ROOT / "feature_list.json"
DEPS_FILE = ROOT / "tools" / "feature_deps.json"
RUNTIME_FILE = ROOT / "tools" / ".agent_runtime.json"
LOCK_FILE = ROOT / "tools" / ".agent_pool.lock"


# ----------------------------------------------------------------------------
# Lock + atomic IO
# ----------------------------------------------------------------------------
class Lock:
    """Exclusive fcntl lock; serializes every read-modify-write of pool state."""

    def __enter__(self):
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(LOCK_FILE, "w")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        self._fd.close()


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


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def load_deps() -> dict:
    return load_json(DEPS_FILE, {})


def save_deps(d: dict) -> None:
    _atomic_write(DEPS_FILE, json.dumps(d, indent=2, sort_keys=True) + "\n")


def load_runtime() -> dict:
    rt = load_json(RUNTIME_FILE, {"leases": {}})
    rt.setdefault("leases", {})
    return rt


def save_runtime(rt: dict) -> None:
    _atomic_write(RUNTIME_FILE, json.dumps(rt, indent=2, sort_keys=True) + "\n")


# ----------------------------------------------------------------------------
# Feature truth (prefer integrated origin/main; fall back to local working file)
# ----------------------------------------------------------------------------
def load_features(fetch: bool = False) -> list:
    if fetch:
        _run(["git", "-C", str(ROOT), "fetch", "--quiet", "origin"], check=False)
    try:
        raw = _run(["git", "-C", str(ROOT), "show", "origin/main:feature_list.json"]).stdout
        return json.loads(raw)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return load_json(FEATURE_FILE, [])


def subsystem(feat: dict) -> str:
    return CATEGORY_SUBSYSTEM.get(feat.get("category", ""), feat.get("category", "?"))


def needs_serialized(feat: dict) -> tuple[bool, list[str]]:
    """Heuristic: does this feature's text imply IB/integration/live/e2e steps?"""
    hay = (" " + feat.get("description", "") + " " + " ".join(feat.get("steps", [])) + " ").lower()
    hits = [kw.strip() for kw in SERIALIZED_KEYWORDS if kw in hay]
    return (bool(hits), hits)


def base_ref() -> str:
    if (
        _run(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", "origin/main"],
            check=False,
        ).returncode
        == 0
    ):
        return "origin/main"
    return "main"


# ----------------------------------------------------------------------------
# Lease liveness (don't reclaim a feature whose process is still alive)
# ----------------------------------------------------------------------------
def owner_host(owner: str) -> str:
    """The host portion of a 'host:pid' owner string ('' if malformed)."""
    if not owner or ":" not in owner:
        return ""
    return owner.rpartition(":")[0]


def owner_is_live(owner: str) -> bool:
    """True if owner is 'host:pid', host is THIS host, and that pid is alive."""
    host = owner_host(owner)
    if not host or host != socket.gethostname():
        return False  # can't probe a remote host's pid
    try:
        pid = int(owner.rpartition(":")[2])
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive but not ours
    return True


def lease_active(lease: dict, now: float, *, allow_foreign_reclaim: bool = False) -> bool:
    """Does a lease still hold?

    * Same-host owner with a live PID  -> active (even past TTL).
    * Foreign-host owner               -> sticky-active (we cannot probe a remote
      PID, so never auto-reclaim on TTL alone) unless ``allow_foreign_reclaim``.
    * Same-host owner with a dead/absent PID -> governed by the TTL.
    """
    owner = lease.get("owner", "")
    if owner_is_live(owner):
        return True
    host = owner_host(owner)
    if host and host != socket.gethostname():
        return not allow_foreign_reclaim  # remote: sticky unless explicitly reclaiming
    return lease.get("expiry", 0) > now


def lease_blocks_owner(lease: dict, our_owner: str, now: float) -> bool:
    """True if an ACTIVE lease is held by a DIFFERENT owner — i.e. we must not
    integrate/act on this feature (a sibling owns it)."""
    return bool(lease) and lease.get("owner") != our_owner and lease_active(lease, now)


def should_refuse_release(lease: dict, our_owner: str, force: bool, now: float) -> bool:
    """Refuse `release` of an active lease held by a different owner unless forced."""
    return (not force) and lease_blocks_owner(lease, our_owner, now)


def worktree_dirty(wt: Path) -> bool:
    if not wt.exists():
        return False
    return bool(_run(["git", "-C", str(wt), "status", "--porcelain"], check=False).stdout.strip())


# ----------------------------------------------------------------------------
# Scheduling core
# ----------------------------------------------------------------------------
def compute(features, deps, runtime, *, allow_foreign_reclaim=False):
    """Return (ready, blocked, active_leases, held_subsystems, by_id)."""
    by_id = {f["id"]: f for f in features}
    passed = {fid for fid, f in by_id.items() if f.get("passes") is True}
    now = time.time()
    active = {
        fid: lease
        for fid, lease in runtime["leases"].items()
        if lease_active(lease, now, allow_foreign_reclaim=allow_foreign_reclaim)
    }
    held = {subsystem(by_id[fid]) for fid in active if fid in by_id}

    ready, blocked = [], {}
    for fid, f in by_id.items():
        if f.get("passes") is True or f.get("needs_clarification") is True:
            continue
        if fid in active:
            continue
        unmet = [d for d in deps.get(fid, []) if d not in passed and d in by_id]
        if unmet:
            blocked[fid] = unmet
        else:
            ready.append(fid)
    return ready, blocked, active, held, by_id


def impact_scores(deps: dict, by_id: dict) -> dict:
    """Map feature id -> how many *other* features it (transitively) unblocks.

    The dependency map is ``{feature: [prerequisites]}``. Its reverse tells us,
    for a prerequisite ``p``, every feature that (directly or transitively)
    depends on ``p`` — i.e. the work ``p`` unlocks. Higher = more of a keystone.
    Used to steer the greedy scheduler toward features that open the most
    downstream work instead of the alphabetically-first leaf.
    """
    rev: dict = {}
    for f, prereqs in deps.items():
        for p in prereqs:
            rev.setdefault(p, set()).add(f)

    def closure(x: str) -> set:
        seen: set = set()
        stack = list(rev.get(x, ()))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(rev.get(cur, ()))
        return seen

    return {fid: len(closure(fid)) for fid in by_id}


def pick_order(ready, by_id, held, impact=None):
    """Ready features ordered by: (1) subsystem no active sibling lease holds —
    to avoid merge conflicts; (2) most downstream features unblocked (keystones
    first); (3) priority; (4) id. ``impact`` defaults to all-zero (pure legacy
    order) when not supplied."""
    impact = impact or {}
    return sorted(
        ready,
        key=lambda fid: (
            subsystem(by_id[fid]) in held,  # False (0) sorts before True (1)
            -impact.get(fid, 0),  # more-unblocking keystone first
            by_id[fid].get("priority", "P9"),
            fid,
        ),
    )


def serialized_notes(progress_dir: Path | None = None) -> set:
    """Feature ids whose ``progress.d/session-<id>.md`` records ``Outcome:
    serialized`` — code is done but ≥1 step needs human IB/e2e verification.

    Re-offering such a feature to a fresh agent is the churn loop: it can only
    ever integrate ``serialized`` again (never ``complete``), so it returns to
    the ready frontier forever. We exclude these from claiming and surface them
    as an ``awaiting_verification`` bucket for the operator to close by hand.
    """
    progress_dir = progress_dir or (ROOT / "progress.d")
    out: set = set()
    if not progress_dir.is_dir():
        return out
    for note in progress_dir.glob("session-*.md"):
        fid = note.stem[len("session-") :]
        try:
            text = note.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            s = line.strip().lower()
            if not s.startswith("outcome:"):
                continue
            # Value after "outcome:"; the template menu line starts with
            # "complete | ..." so only a real serialized outcome matches.
            if s.split("outcome:", 1)[1].strip().startswith("serialized"):
                out.add(fid)
            break
    return out


def assess_frontier(features, deps, runtime, *, skip_awaiting=True) -> dict:
    """Classify the board as done / progressing / deadlock and name the keystones.

    This is what tells "the application is finished" apart from "the scheduler is
    stuck": ``claim`` returns ``EMPTY`` for both, but only one needs operator
    action. ``deadlock`` means nothing is claimable yet features remain — the
    ``root_blockers`` (ranked by impact) are the not-passing prerequisites the
    blocked set waits on, and ``guarded_root_blockers`` are the subset that match
    the IB/integration/e2e honesty guard and so can *only* be closed by a human
    ``integrate --force-complete`` or the ``verified-e2e`` label.
    """
    ready, blocked, active, held, by_id = compute(features, deps, runtime)
    total = len(by_id)
    passed = [fid for fid, f in by_id.items() if f.get("passes") is True]
    awaiting = sorted(serialized_notes() & set(ready)) if skip_awaiting else []
    awaiting_set = set(awaiting)
    claimable = [fid for fid in ready if fid not in awaiting_set]

    if len(passed) == total:
        state = "done"
    elif claimable:
        state = "progressing"
    else:
        state = "deadlock"

    impact = impact_scores(deps, by_id)
    blockers = sorted(
        {d for unmet in blocked.values() for d in unmet},
        key=lambda d: (-impact.get(d, 0), d),
    )
    guarded = [d for d in blockers if d in by_id and needs_serialized(by_id[d])[0]]
    return {
        "state": state,
        "total": total,
        "passed": len(passed),
        "ready": claimable,
        "awaiting_verification": awaiting,
        "blocked": blocked,
        "root_blockers": blockers,
        "guarded_root_blockers": guarded,
        "active": active,
    }


def free_port_index(active) -> int:
    used = {lease.get("port_index") for lease in active.values()}
    idx = 0
    while idx in used:
        idx += 1
    return idx


def ports_for(idx: int) -> dict:
    return {
        "ATP_DEV_PORT": DEV_BASE + idx * PORT_STRIDE,
        "ATP_IB_LIVE_PORT": IB_LIVE_BASE + idx * PORT_STRIDE,
        "ATP_IB_PAPER_PORT": IB_PAPER_BASE + idx * PORT_STRIDE,
    }


def branch_exists(branch: str) -> bool:
    return (
        _run(
            ["git", "-C", str(ROOT), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
        ).returncode
        == 0
    )


def reachable(deps: dict, src: str, dst: str) -> bool:
    """Is dst reachable from src by following deps edges? (cycle detection)"""
    seen, stack = set(), [src]
    while stack:
        cur = stack.pop()
        if cur == dst:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(deps.get(cur, []))
    return False


def validate_block(ids: set, fid: str, on: list) -> tuple[list, list]:
    """Split --on into (known, unknown) ids; fid is validated by the caller."""
    known = [d for d in on if d in ids]
    unknown = [d for d in on if d not in ids]
    return known, unknown


# ----------------------------------------------------------------------------
# Subcommands
# ----------------------------------------------------------------------------
def cmd_seed(args):
    with Lock():
        features = load_features(fetch=False)
        ids = {f["id"] for f in features}
        deps = load_deps()
        added = 0
        for fid, prereqs in SEED_DEPS.items():
            if fid not in ids:
                continue
            cur = set(deps.get(fid, []))
            for p in prereqs:
                if p in ids and p not in cur and not reachable(deps, p, fid):
                    cur.add(p)
                    added += 1
            if cur:
                deps[fid] = sorted(cur)
        save_deps(deps)
    print(
        f"✓ seeded {DEPS_FILE.relative_to(ROOT)} (+{added} edges, {len(deps)} features with deps)"
    )
    return 0


def cmd_status(args):
    features = load_features(fetch=not args.no_fetch)
    deps = load_deps()
    runtime = load_runtime()
    ready, blocked, active, held, by_id = compute(features, deps, runtime)

    skip_awaiting = not getattr(args, "include_awaiting", False)
    awaiting = sorted(serialized_notes() & set(ready)) if skip_awaiting else []
    awaiting_set = set(awaiting)
    claimable = [fid for fid in ready if fid not in awaiting_set]
    impact = impact_scores(deps, by_id)
    assessment = assess_frontier(features, deps, runtime, skip_awaiting=skip_awaiting)

    if args.json:
        print(
            json.dumps(
                {
                    "assessment": assessment["state"],
                    "ready": sorted(claimable),
                    "awaiting_verification": awaiting,
                    "blocked": {k: v for k, v in sorted(blocked.items())},
                    "leases": active,
                    "done": sorted(f for f, x in by_id.items() if x.get("passes")),
                    "root_blockers": assessment["root_blockers"],
                    "guarded_root_blockers": assessment["guarded_root_blockers"],
                },
                indent=2,
            )
        )
        return 0

    done = sum(1 for f in by_id.values() if f.get("passes"))
    print(
        f"== agent pool == done:{done}/{assessment['total']}  ready:{len(claimable)}  "
        f"awaiting-verify:{len(awaiting)}  blocked:{len(blocked)}  leased:{len(active)}"
    )
    print(f"   frontier: {assessment['state'].upper()}")
    if assessment["state"] == "deadlock" and assessment["root_blockers"]:
        guarded = assessment["guarded_root_blockers"]
        print(
            "   ⚠ no autonomous progress possible — highest-impact blockers: "
            + ", ".join(assessment["root_blockers"][:5])
        )
        if guarded:
            print(
                "   these need a human `integrate --force-complete` / verified-e2e: "
                + ", ".join(guarded[:5])
            )
    if active:
        print("\n-- in progress (leased) --")
        for fid, lease in sorted(active.items()):
            left = int(lease.get("expiry", 0) - time.time())
            alive = "alive" if owner_is_live(lease.get("owner", "")) else f"{left // 60}m left"
            print(
                f"  {fid:18} by {lease.get('owner', '?'):22} port#{lease.get('port_index', '?')}  {alive}"
            )
    print("\n-- ready frontier (most-unblocking first) --")
    for fid in pick_order(claimable, by_id, held, impact):
        f = by_id[fid]
        print(
            f"  {fid:18} {f.get('priority', '?'):3} unblocks:{impact.get(fid, 0):<3} "
            f"{subsystem(f):20} {f['description'][:40]}"
        )
    if awaiting:
        print("\n-- awaiting human verification (serialized; not re-offered) --")
        for fid in awaiting:
            print(f"  {fid:18} {by_id[fid]['description'][:52]}")
    if blocked:
        print("\n-- blocked (waiting on deps) --")
        for fid, unmet in sorted(blocked.items()):
            print(f"  {fid:18} blocked-on {', '.join(unmet)}")
    nc = [f for f, x in by_id.items() if x.get("needs_clarification")]
    if nc:
        print("\n-- needs clarification (operator) --")
        for fid in sorted(nc):
            print(f"  {fid}")
    return 0


def cmd_claim(args):
    with Lock():
        features = load_features(fetch=True)
        deps = load_deps()
        runtime = load_runtime()
        ready, blocked, active, held, by_id = compute(
            features, deps, runtime, allow_foreign_reclaim=args.reclaim
        )

        # Steer toward keystones (most-unblocking first) and skip features that
        # are code-done but awaiting human e2e verification (the churn loop).
        impact = impact_scores(deps, by_id)
        skip_awaiting = not getattr(args, "include_awaiting", False)
        awaiting = serialized_notes() & set(ready) if skip_awaiting else set()
        claimable = [fid for fid in ready if fid not in awaiting]

        choice = None
        skipped_dirty = []
        for fid in pick_order(claimable, by_id, held, impact):
            wt = ROOT.parent / f"alphalabs-wt-{fid}"
            # A stale (reclaimed) worktree with uncommitted work isn't safe to
            # silently reuse — skip it unless the operator forces --reclaim.
            if wt.exists() and worktree_dirty(wt) and not args.reclaim:
                skipped_dirty.append(fid)
                continue
            choice = fid
            break

        if choice is None:
            print("FEATURE=EMPTY")
            assessment = assess_frontier(features, deps, runtime, skip_awaiting=skip_awaiting)
            note = []
            if assessment["state"] == "done":
                note.append("ALL features pass — the application is complete. 🎉")
            elif assessment["state"] == "deadlock":
                note.append(
                    "DEADLOCK — no autonomous progress possible; every remaining "
                    "feature is blocked or awaiting human verification"
                )
                if assessment["guarded_root_blockers"]:
                    note.append(
                        "verify + `integrate --force-complete` (or verified-e2e label): "
                        + ", ".join(assessment["guarded_root_blockers"][:5])
                    )
                elif assessment["root_blockers"]:
                    note.append(
                        "highest-impact blockers: " + ", ".join(assessment["root_blockers"][:5])
                    )
            if awaiting:
                note.append(
                    f"{len(awaiting)} awaiting human verification (serialized): "
                    + ", ".join(sorted(awaiting))
                )
            if skipped_dirty:
                note.append(
                    f"{len(skipped_dirty)} ready but dirty stale worktree(s): "
                    f"{', '.join(skipped_dirty)} (re-run with --reclaim)"
                )
            print(
                "# " + ("; ".join(note) if note else "no ready feature to claim."), file=sys.stderr
            )
            return 0

        idx = free_port_index(active)
        owner = os.environ.get("ATP_AGENT_OWNER") or f"{socket.gethostname()}:{os.getpid()}"
        runtime["leases"][choice] = {
            "owner": owner,
            "ts": int(time.time()),
            "expiry": int(time.time() + LEASE_TTL),
            "port_index": idx,
        }

        # Create (or reuse) the worktree + branch under the lock so two claimers
        # never race on branch creation.
        wt = ROOT.parent / f"alphalabs-wt-{choice}"
        branch = f"agent/{choice}"
        base = base_ref()
        if not wt.exists():
            if branch_exists(branch):
                _run(["git", "-C", str(ROOT), "worktree", "add", str(wt), branch])
            else:
                _run(["git", "-C", str(ROOT), "worktree", "add", "-b", branch, str(wt), base])

        save_runtime(runtime)

    p = ports_for(idx)
    print(f"FEATURE={choice}")
    print(f"WORKTREE={wt}")
    print(f"BRANCH={branch}")
    for k, v in p.items():
        print(f"{k}={v}")
    return 0


def cmd_block(args):
    fid = args.id
    with Lock():
        ids = {f["id"] for f in load_features(fetch=False)}
        if fid not in ids:
            print(f"✗ unknown feature id: {fid}", file=sys.stderr)
            return 1
        deps = load_deps()
        known, unknown = validate_block(ids, fid, args.on)
        if unknown:
            for u in unknown:
                hint = difflib.get_close_matches(u, ids, n=2)
                print(
                    f"✗ unknown dependency id: {u}" + (f" (did you mean {hint}?)" if hint else ""),
                    file=sys.stderr,
                )
            return 1
        cur = set(deps.get(fid, []))
        cycles = []
        for dep in known:
            if dep == fid or reachable(deps, dep, fid):
                cycles.append(dep)
                continue
            cur.add(dep)
        if cur:
            deps[fid] = sorted(cur)
        save_deps(deps)
        # NOTE: block does NOT release the lease — you keep ownership until you
        # `integrate --mode partial` (which releases it on success). Releasing
        # here would open a window where a sibling could claim the same worktree
        # before your partial work lands.
    print(
        f"✓ {fid} blocked-on {sorted(set(known) - set(cycles))}; lease kept "
        f"(release it via `integrate --mode partial` or `release {fid}`)"
    )
    if cycles:
        print(
            f"⚠ skipped (would create dependency cycle): {cycles} — resolve manually",
            file=sys.stderr,
        )
    return 0


def _sync_deps_into(wt: Path) -> None:
    """Copy the canonical deps file into a worktree so it lands on main."""
    if DEPS_FILE.exists():
        (wt / "tools").mkdir(parents=True, exist_ok=True)
        (wt / "tools" / "feature_deps.json").write_text(
            DEPS_FILE.read_text(encoding="utf-8"), encoding="utf-8"
        )


def _has_staged(wt: Path) -> bool:
    return _run(["git", "-C", str(wt), "diff", "--cached", "--quiet"], check=False).returncode != 0


def path_in_allowlist(path: str) -> bool:
    """True if a repo-relative path is within the integrate allowlist."""
    return any(path == p or path.startswith(p + "/") for p in INTEGRATE_ALLOWLIST)


def porcelain_outside_allowlist(porcelain: str) -> list[str]:
    """Parse `git status --porcelain` text; return changed paths outside the
    allowlist, checking BOTH sides of a rename (`old -> new`)."""
    bad = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        rest = line[3:]
        parts = rest.split(" -> ") if " -> " in rest else [rest]
        for p in parts:
            p = p.strip().strip('"')
            if p and not path_in_allowlist(p):
                bad.append(p)
    return bad


def _uncommitted_outside_allowlist(wt: Path) -> list[str]:
    """Worktree paths with uncommitted changes that integrate must not touch."""
    return porcelain_outside_allowlist(
        _run(["git", "-C", str(wt), "status", "--porcelain"], check=False).stdout
    )


def staged_outside_allowlist(names: list[str]) -> list[str]:
    """Staged path names that fall outside the allowlist."""
    return [p for p in names if p and not path_in_allowlist(p)]


def _staged_paths(wt: Path) -> list[str]:
    out = _run(
        ["git", "-C", str(wt), "diff", "--cached", "--name-only", "--no-renames", "-z"],
        check=False,
    ).stdout
    return [p for p in out.split("\0") if p]


def _drop_prior_integration_commit(wt: Path) -> str:
    """Drop an unpushed [agent-integrate] HEAD commit so we can recompute.

    Returns "none" if HEAD is not a marker commit, "dropped" if it was safely
    reset, or "refused" if it is a marker commit that touches paths outside the
    allowlist (a safety stop — it must never have contained feature work).
    """
    msg = _run(["git", "-C", str(wt), "log", "-1", "--format=%s"], check=False).stdout
    if INTEGRATE_MARKER not in msg:
        return "none"
    touched = [
        p
        for p in _run(
            ["git", "-C", str(wt), "diff", "--name-only", "HEAD~1", "HEAD"], check=False
        ).stdout.splitlines()
        if p
    ]
    if any(not path_in_allowlist(p) for p in touched):
        return "refused"
    _run(["git", "-C", str(wt), "reset", "--hard", "HEAD~1"], check=False)
    return "dropped"


def shared_state_violations(committed_paths: list[str], fid: str) -> list[str]:
    """Committed (base...HEAD) branch paths that only the integrator may write.

    The agent may commit just its own resume note progress.d/session-<fid>.md;
    feature_list.json / progress.txt / tools/feature_deps.json and any other
    progress.d/* must come solely from the integrator's marker commit.
    """
    own_note = f"progress.d/session-{fid}.md"
    permanent = {"progress.d/README.md", "progress.d/.gitkeep"}
    bad = []
    for p in committed_paths:
        if p in ("feature_list.json", "progress.txt", "tools/feature_deps.json"):
            bad.append(p)
        elif p.startswith("progress.d/") and p != own_note and p not in permanent:
            bad.append(p)
    return bad


def _branch_committed(wt: Path) -> list[str]:
    out = _run(
        ["git", "-C", str(wt), "diff", "--name-only", "--no-renames", f"{base_ref()}...HEAD"],
        check=False,
    ).stdout
    return [p for p in out.splitlines() if p]


def cmd_integrate(args):
    fid = args.id
    mode = args.mode
    wt = ROOT.parent / f"alphalabs-wt-{fid}"
    if not wt.exists():
        print(f"✗ worktree {wt} not found", file=sys.stderr)
        return 1

    # Honesty guard (code-enforced backstop): refuse `complete` for a feature
    # whose text implies IB/integration/live/e2e steps unless forced.
    if mode == "complete" and not args.force_complete:
        feat = next((x for x in load_features(fetch=False) if x["id"] == fid), None)
        if feat:
            need, hits = needs_serialized(feat)
            if need:
                print(
                    f"✗ {fid}: steps imply non-solo verification {hits} → use --mode serialized "
                    f"(or --force-complete if you genuinely verified every step solo).",
                    file=sys.stderr,
                )
                return 5

    if args.dry_run:
        ahead = _run(
            ["git", "-C", str(wt), "rev-list", "--count", f"{base_ref()}..HEAD"], check=False
        ).stdout.strip()
        print(
            f"[dry-run] {fid} mode={mode}: {ahead} commit(s) ahead of {base_ref()}; "
            f"would rebase, {'flip passes:true + ' if mode == 'complete' else ''}push HEAD:main, release lease"
        )
        return 0

    # Refuse to integrate with uncommitted work outside the integration scope —
    # otherwise it would be swept into the marker commit and lost on a retry.
    dirty = _uncommitted_outside_allowlist(wt)
    if dirty:
        shown = ", ".join(dirty[:6]) + ("..." if len(dirty) > 6 else "")
        print(
            f"✗ {fid}: uncommitted changes outside integration scope ({shown}). "
            f"Commit your feature/test work first (Step 7), then re-run integrate.",
            file=sys.stderr,
        )
        return 7

    with Lock():
        # Ownership: refuse to integrate a feature an active sibling lease holds.
        our_owner = os.environ.get("ATP_AGENT_OWNER") or f"{socket.gethostname()}:{os.getpid()}"
        lease = load_runtime()["leases"].get(fid)
        if lease_blocks_owner(lease, our_owner, time.time()):
            print(
                f"✗ {fid}: leased by another active session ({lease.get('owner')}); "
                f"refusing to integrate. Use `release {fid}` only if it is genuinely stale.",
                file=sys.stderr,
            )
            return 10
        push_err = ""
        ok = False
        for _attempt in (1, 2):
            if _drop_prior_integration_commit(wt) == "refused":
                print(
                    f"✗ {fid}: HEAD is an [agent-integrate] commit touching files outside the "
                    f"allowlist — refusing to reset (manual review needed).",
                    file=sys.stderr,
                )
                return 8
            _run(["git", "-C", str(wt), "fetch", "--quiet", "origin"], check=False)
            rb = _run(["git", "-C", str(wt), "rebase", base_ref()], check=False)
            if rb.returncode != 0:
                _run(["git", "-C", str(wt), "rebase", "--abort"], check=False)
                print(
                    f"✗ {fid}: rebase onto {base_ref()} conflicted — left branch for manual resolution",
                    file=sys.stderr,
                )
                return 3

            # Defense-in-depth: the branch's own commits must not mutate shared
            # coordination state — only the integrator's marker commit may (this
            # holds for complete AND partial/serialized).
            violations = shared_state_violations(_branch_committed(wt), fid)
            if violations:
                print(
                    f"✗ {fid}: branch commits modify shared coordination files {violations} — "
                    f"only the integrator may write them. Revert those commits.",
                    file=sys.stderr,
                )
                return 6

            # Recompute the flip against the just-rebased (latest main) tree, so the
            # close commit is fresh each attempt — never rebased, so a concurrent
            # flip on main can't conflict on the whole-file feature_list rewrite.
            _sync_deps_into(wt)
            if mode == "complete":
                _run([sys.executable, str(wt / "tools" / "close_feature.py"), fid, "--verified"])

            # Stage ONLY the integration allowlist (never `git add -A`), so the
            # marker commit can never contain feature work.
            existing = [p for p in INTEGRATE_ALLOWLIST if (wt / p).exists()]
            if existing:
                _run(["git", "-C", str(wt), "add", "-A", "--", *existing])
            # Final assertion before committing: nothing outside the allowlist may
            # be staged (e.g. a pre-staged rename source riding in the index).
            outside = staged_outside_allowlist(_staged_paths(wt))
            if outside:
                print(
                    f"✗ {fid}: refusing — staged changes outside the integration allowlist: "
                    f"{outside}. Unstage them and re-run.",
                    file=sys.stderr,
                )
                return 9
            if _has_staged(wt):
                tag = (
                    "verified e2e — flip passes:true + fold note"
                    if mode == "complete"
                    else f"{mode} — synced deps/notes (passes stays false)"
                )
                _run(
                    [
                        "git",
                        "-C",
                        str(wt),
                        "commit",
                        "-m",
                        f"chore({fid}): {tag} {INTEGRATE_MARKER}",
                    ]
                )

            push = _run(["git", "-C", str(wt), "push", "origin", "HEAD:main"], check=False)
            if push.returncode == 0:
                ok = True
                break
            push_err = push.stderr  # non-fast-forward: loop re-fetches/rebases/recomputes

        if not ok:
            print(
                f"✗ {fid}: push to main failed after retry (non-fast-forward?):\n{push_err}\n"
                f"  branch left intact; re-run `integrate` to retry safely (idempotent).",
                file=sys.stderr,
            )
            return 4

        runtime = load_runtime()
        runtime["leases"].pop(fid, None)
        save_runtime(runtime)

    print(f"✓ integrated {fid} (mode={mode}) → origin/main; lease released")
    return 0


def cmd_heartbeat(args):
    with Lock():
        runtime = load_runtime()
        lease = runtime["leases"].get(args.id)
        if not lease:
            print(f"✗ no lease for {args.id}", file=sys.stderr)
            return 1
        lease["expiry"] = int(time.time() + LEASE_TTL)
        save_runtime(runtime)
    print(f"✓ heartbeat {args.id} (+{LEASE_TTL // 60}m)")
    return 0


def cmd_release(args):
    with Lock():
        runtime = load_runtime()
        lease = runtime["leases"].get(args.id)
        if lease is None:
            print(f"· {args.id} had no lease")
            return 0
        our_owner = os.environ.get("ATP_AGENT_OWNER") or f"{socket.gethostname()}:{os.getpid()}"
        if should_refuse_release(lease, our_owner, args.force, time.time()):
            print(
                f"✗ {args.id}: held by another active session ({lease.get('owner')}); "
                f"refusing to release. Pass --force only if it is genuinely stale.",
                file=sys.stderr,
            )
            return 1
        runtime["leases"].pop(args.id, None)
        save_runtime(runtime)
    print(f"✓ released {args.id}")
    return 0


# ----------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description="Locked dependency-aware agent scheduler.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("seed", help="populate feature_deps.json from curated edges (idempotent)")

    sp = sub.add_parser("status", help="show the board (ready/blocked/leased/done)")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--no-fetch", action="store_true", help="skip git fetch (offline/fast)")
    sp.add_argument(
        "--include-awaiting",
        action="store_true",
        help="count serialized/awaiting-verification features as ready (default: excluded)",
    )

    cp = sub.add_parser(
        "claim", help="claim the best ready feature; create its worktree; print env"
    )
    cp.add_argument("--reclaim", action="store_true", help="reuse a stale worktree even if dirty")
    cp.add_argument(
        "--include-awaiting",
        action="store_true",
        help="allow claiming serialized/awaiting-verification features (default: skipped)",
    )

    bp = sub.add_parser("block", help="record discovered dependency edge(s) + release lease")
    bp.add_argument("id")
    bp.add_argument("--on", nargs="+", required=True, metavar="DEP_ID")
    bp.add_argument("--reason", default="")

    ip = sub.add_parser("integrate", help="rebase+merge to main; flip passes on complete")
    ip.add_argument("id")
    ip.add_argument("--mode", choices=["complete", "partial", "serialized"], required=True)
    ip.add_argument(
        "--force-complete",
        action="store_true",
        help="override the IB/integration honesty guard (you verified every step solo)",
    )
    ip.add_argument("--dry-run", action="store_true")

    hp = sub.add_parser("heartbeat", help="extend a lease")
    hp.add_argument("id")

    rp = sub.add_parser("release", help="drop a lease")
    rp.add_argument("id")
    rp.add_argument(
        "--force", action="store_true", help="release even an active lease owned by another session"
    )

    args = p.parse_args()
    return {
        "seed": cmd_seed,
        "status": cmd_status,
        "claim": cmd_claim,
        "block": cmd_block,
        "integrate": cmd_integrate,
        "heartbeat": cmd_heartbeat,
        "release": cmd_release,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
