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


def pick_order(ready, by_id, held):
    """Ready features ordered: subsystem no lease holds first, then priority, id."""
    return sorted(
        ready,
        key=lambda fid: (
            subsystem(by_id[fid]) in held,  # False (0) sorts before True (1)
            by_id[fid].get("priority", "P9"),
            fid,
        ),
    )


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

    if args.json:
        print(
            json.dumps(
                {
                    "ready": sorted(ready),
                    "blocked": {k: v for k, v in sorted(blocked.items())},
                    "leases": active,
                    "done": sorted(f for f, x in by_id.items() if x.get("passes")),
                },
                indent=2,
            )
        )
        return 0

    done = sum(1 for f in by_id.values() if f.get("passes"))
    print(
        f"== agent pool == done:{done}  ready:{len(ready)}  "
        f"blocked:{len(blocked)}  leased:{len(active)}"
    )
    if active:
        print("\n-- in progress (leased) --")
        for fid, lease in sorted(active.items()):
            left = int(lease.get("expiry", 0) - time.time())
            alive = "alive" if owner_is_live(lease.get("owner", "")) else f"{left // 60}m left"
            print(
                f"  {fid:18} by {lease.get('owner', '?'):22} port#{lease.get('port_index', '?')}  {alive}"
            )
    print("\n-- ready frontier --")
    for fid in pick_order(ready, by_id, held):
        f = by_id[fid]
        print(f"  {fid:18} {f.get('priority', '?'):3} {subsystem(f):20} {f['description'][:44]}")
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

        choice = None
        skipped_dirty = []
        for fid in pick_order(ready, by_id, held):
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
            note = []
            if skipped_dirty:
                note.append(
                    f"{len(skipped_dirty)} ready but dirty stale worktree(s): "
                    f"{', '.join(skipped_dirty)} (re-run with --reclaim)"
                )
            if blocked:
                note.append(
                    f"{len(blocked)} blocked: "
                    + "; ".join(f"{k}<-{','.join(v)}" for k, v in sorted(blocked.items()))
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
        runtime = load_runtime()
        runtime["leases"].pop(fid, None)
        save_runtime(runtime)
    print(f"✓ {fid} blocked-on {sorted(set(known) - set(cycles))}; lease released")
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


def _uncommitted_outside_allowlist(wt: Path) -> list[str]:
    """Worktree paths with uncommitted changes that integrate must not touch."""
    out = _run(["git", "-C", str(wt), "status", "--porcelain"], check=False).stdout
    bad = []
    for line in out.splitlines():
        path = line[3:].strip().strip('"')
        if " -> " in path:  # rename: take the destination
            path = path.split(" -> ", 1)[1]
        if path and not path_in_allowlist(path):
            bad.append(path)
    return bad


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


def _branch_touches_feature_list(wt: Path) -> bool:
    return (
        _run(
            [
                "git",
                "-C",
                str(wt),
                "diff",
                "--quiet",
                f"{base_ref()}...HEAD",
                "--",
                "feature_list.json",
            ],
            check=False,
        ).returncode
        != 0
    )


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

            if mode == "complete":
                # Recompute the flip against the just-rebased (latest main) tree,
                # so the close commit is fresh each attempt — never rebased, so a
                # concurrent flip on main can't conflict on the whole-file rewrite.
                _sync_deps_into(wt)
                _run([sys.executable, str(wt / "tools" / "close_feature.py"), fid, "--verified"])
            else:  # partial | serialized — must NOT flip passes
                if _branch_touches_feature_list(wt):
                    print(
                        f"✗ {fid}: {mode} mode but the branch modifies feature_list.json — "
                        f"only --mode complete may flip passes. Revert that change.",
                        file=sys.stderr,
                    )
                    return 6
                _sync_deps_into(wt)

            # Stage ONLY the integration allowlist (never `git add -A`), so the
            # marker commit can never contain feature work.
            existing = [p for p in INTEGRATE_ALLOWLIST if (wt / p).exists()]
            if existing:
                _run(["git", "-C", str(wt), "add", "-A", "--", *existing])
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
        existed = runtime["leases"].pop(args.id, None) is not None
        save_runtime(runtime)
    print(f"✓ released {args.id}" if existed else f"· {args.id} had no lease")
    return 0


# ----------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description="Locked dependency-aware agent scheduler.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("seed", help="populate feature_deps.json from curated edges (idempotent)")

    sp = sub.add_parser("status", help="show the board (ready/blocked/leased/done)")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--no-fetch", action="store_true", help="skip git fetch (offline/fast)")

    cp = sub.add_parser(
        "claim", help="claim the best ready feature; create its worktree; print env"
    )
    cp.add_argument("--reclaim", action="store_true", help="reuse a stale worktree even if dirty")

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
