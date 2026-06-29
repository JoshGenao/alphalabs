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
  (``{"leases": {id: {owner, ts, expiry, port_index}}}``).
* ``tools/.agent_pool.lock``    — gitignored ``fcntl.flock`` mutex (macOS lacks
  the ``flock`` binary, so all locking is done here in Python).

``passes`` truth is read from ``origin/main:feature_list.json`` (the integrated
state), falling back to the local working file when offline.

Subcommands: ``seed``, ``status``, ``claim``, ``block``, ``integrate``,
``heartbeat``, ``release``. See ``prompts/coding_prompt.md`` and AGENTS.md.
"""

from __future__ import annotations

import argparse
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
        raw = _run(
            ["git", "-C", str(ROOT), "show", "origin/main:feature_list.json"]
        ).stdout
        return json.loads(raw)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return load_json(FEATURE_FILE, [])


def subsystem(feat: dict) -> str:
    return CATEGORY_SUBSYSTEM.get(feat.get("category", ""), feat.get("category", "?"))


def base_ref() -> str:
    if _run(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", "origin/main"],
        check=False,
    ).returncode == 0:
        return "origin/main"
    return "main"


# ----------------------------------------------------------------------------
# Scheduling core
# ----------------------------------------------------------------------------
def compute(features, deps, runtime):
    """Return (ready, blocked, live_leases, held_subsystems, by_id)."""
    by_id = {f["id"]: f for f in features}
    passed = {fid for fid, f in by_id.items() if f.get("passes") is True}
    now = time.time()
    live = {fid: l for fid, l in runtime["leases"].items() if l.get("expiry", 0) > now}
    held = {subsystem(by_id[fid]) for fid in live if fid in by_id}

    ready, blocked = [], {}
    for fid, f in by_id.items():
        if f.get("passes") is True or f.get("needs_clarification") is True:
            continue
        if fid in live:
            continue
        unmet = [d for d in deps.get(fid, []) if d not in passed and d in by_id]
        if unmet:
            blocked[fid] = unmet
        else:
            ready.append(fid)
    return ready, blocked, live, held, by_id


def pick(ready, by_id, held):
    """Prefer a feature whose subsystem no live lease holds; then priority, id."""
    if not ready:
        return None
    return sorted(
        ready,
        key=lambda fid: (
            subsystem(by_id[fid]) in held,  # False (0) sorts before True (1)
            by_id[fid].get("priority", "P9"),
            fid,
        ),
    )[0]


def free_port_index(live) -> int:
    used = {l.get("port_index") for l in live.values()}
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
    print(f"✓ seeded {DEPS_FILE.relative_to(ROOT)} (+{added} edges, {len(deps)} features with deps)")
    return 0


def cmd_status(args):
    features = load_features(fetch=not args.no_fetch)
    deps = load_deps()
    runtime = load_runtime()
    ready, blocked, live, held, by_id = compute(features, deps, runtime)

    if args.json:
        print(
            json.dumps(
                {
                    "ready": sorted(ready),
                    "blocked": {k: v for k, v in sorted(blocked.items())},
                    "leases": live,
                    "done": sorted(f for f, x in by_id.items() if x.get("passes")),
                },
                indent=2,
            )
        )
        return 0

    done = sum(1 for f in by_id.values() if f.get("passes"))
    print(f"== agent pool == done:{done}  ready:{len(ready)}  blocked:{len(blocked)}  leased:{len(live)}")
    if live:
        print("\n-- in progress (leased) --")
        for fid, l in sorted(live.items()):
            left = int(l.get("expiry", 0) - time.time())
            print(f"  {fid:18} by {l.get('owner','?'):20} port#{l.get('port_index','?')}  lease {left//60}m left")
    print("\n-- ready frontier --")
    for fid in sorted(ready, key=lambda x: (by_id[x].get('priority', 'P9'), x)):
        print(f"  {fid:18} {by_id[fid].get('priority','?'):3} {subsystem(by_id[fid]):20} {by_id[fid]['description'][:46]}")
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
        ready, blocked, live, held, by_id = compute(features, deps, runtime)

        choice = pick(ready, by_id, held)
        if choice is None:
            print("FEATURE=EMPTY")
            msg = ["no ready feature to claim."]
            if blocked:
                msg.append(f"{len(blocked)} blocked: " + "; ".join(
                    f"{k}<-{','.join(v)}" for k, v in sorted(blocked.items())))
            print("# " + " ".join(msg), file=sys.stderr)
            return 0

        idx = free_port_index(live)
        owner = os.environ.get("ATP_AGENT_OWNER", f"{socket.gethostname()}:{os.getpid()}")
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
    on = args.on
    with Lock():
        deps = load_deps()
        skipped = []
        cur = set(deps.get(fid, []))
        for dep in on:
            if dep == fid or reachable(deps, dep, fid):
                skipped.append(dep)  # would create a cycle
                continue
            cur.add(dep)
        if cur:
            deps[fid] = sorted(cur)
        save_deps(deps)
        runtime = load_runtime()
        runtime["leases"].pop(fid, None)
        save_runtime(runtime)
    print(f"✓ {fid} blocked-on {sorted(set(on) - set(skipped))}; lease released")
    if skipped:
        print(f"⚠ skipped (would create dependency cycle): {skipped} — resolve manually", file=sys.stderr)
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


def cmd_integrate(args):
    fid = args.id
    mode = args.mode
    wt = ROOT.parent / f"alphalabs-wt-{fid}"
    branch = f"agent/{fid}"
    if not wt.exists():
        print(f"✗ worktree {wt} not found", file=sys.stderr)
        return 1

    with Lock():
        _run(["git", "-C", str(wt), "fetch", "--quiet", "origin"], check=False)
        # Rebase the feature branch onto the latest integrated main.
        rb = _run(["git", "-C", str(wt), "rebase", base_ref()], check=False, capture=True)
        if rb.returncode != 0:
            _run(["git", "-C", str(wt), "rebase", "--abort"], check=False)
            print(f"✗ {fid}: rebase onto {base_ref()} conflicted — left branch for manual resolution", file=sys.stderr)
            return 3

        _sync_deps_into(wt)

        if mode == "complete":
            if args.dry_run:
                print(f"[dry-run] would: close_feature.py {fid} --verified, commit, push HEAD:main")
            else:
                _run([sys.executable, str(wt / "tools" / "close_feature.py"), fid, "--verified"])
                _run(["git", "-C", str(wt), "add", "-A"])
                _run(["git", "-C", str(wt), "commit", "-m",
                      f"chore(close): {fid} verified e2e — flip passes:true + fold note"])
        else:  # partial | serialized
            if args.dry_run:
                print(f"[dry-run] would: commit synced deps/notes ({mode}, passes stays false), push HEAD:main")
            else:
                _run(["git", "-C", str(wt), "add", "-A"])
                if _has_staged(wt):
                    tag = "partial" if mode == "partial" else "serialized-verification-pending"
                    _run(["git", "-C", str(wt), "commit", "-m",
                          f"chore({fid}): {tag} — synced deps/notes (passes stays false)"])

        if args.dry_run:
            print(f"[dry-run] {fid} would integrate (mode={mode}); no push, lease kept")
            return 0

        push = _run(["git", "-C", str(wt), "push", "origin", "HEAD:main"], check=False, capture=True)
        if push.returncode != 0:
            print(f"✗ {fid}: push to main failed (not fast-forward?):\n{push.stderr}", file=sys.stderr)
            return 4

        runtime = load_runtime()
        runtime["leases"].pop(fid, None)
        save_runtime(runtime)

    print(f"✓ integrated {fid} (mode={mode}) → origin/main; lease released")
    return 0


def cmd_heartbeat(args):
    with Lock():
        runtime = load_runtime()
        l = runtime["leases"].get(args.id)
        if not l:
            print(f"✗ no lease for {args.id}", file=sys.stderr)
            return 1
        l["expiry"] = int(time.time() + LEASE_TTL)
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

    sub.add_parser("claim", help="claim the best ready feature; create its worktree; print env")

    bp = sub.add_parser("block", help="record discovered dependency edge(s) + release lease")
    bp.add_argument("id")
    bp.add_argument("--on", nargs="+", required=True, metavar="DEP_ID")
    bp.add_argument("--reason", default="")

    ip = sub.add_parser("integrate", help="rebase+merge to main; flip passes on complete")
    ip.add_argument("id")
    ip.add_argument("--mode", choices=["complete", "partial", "serialized"], required=True)
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
