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

``claim`` auto-picks by default (``tools/claim_and_work.sh``). ``claim --id
<FEATURE_ID>`` instead takes an operator-selected feature, bypassing the
ready-frontier and awaiting-verification filters — those guards stop an
autonomous agent churning on a flip-blocked feature, but a human closing one out
by hand is exactly the case they must not block (``tools/work_on.sh``).
"""

from __future__ import annotations

import argparse
import difflib
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evidence  # noqa: E402  (sibling module in tools/; path set just above)

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
INTEGRATE_ALLOWLIST = (
    "feature_list.json",
    "progress.txt",
    "progress.d",
    "tools/feature_deps.json",
)

# Files ONLY the integrator may author. `integrate` hard-resets these to the base ref
# before writing them, so an agent's local edits — committed or not — cannot reach
# main. progress.d is NOT here: an agent legitimately writes its own session note.
INTEGRATOR_OWNED = ("feature_list.json", "progress.txt")
# NOT in the allowlist: `.harness`. The integrator writes its ledgers to the PRIMARY
# checkout (evidence.LEDGER_DIR), so they never appear in a worktree's git status.
# Allowlisting `.harness` made `git add -A -- .harness` sweep in whatever the agent
# had left uncommitted there — a hand-appended closes.jsonl included — and push it to
# main. shared_state_violations() only inspects COMMITTED paths, so it could not
# catch that: the fix is to keep the ledgers out of the tree the agent controls.

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

# A feature's DECLARED verification_method. This is what the honesty guard reads.
#   solo        — every step verifiable in a parallel session, no shared resource
#   integration — needs real containers / gated I/O (ATP_RUN_INTEGRATION=1)
#   live-ib     — needs the IB Gateway; serialized by the single-live invariant
#   e2e         — needs the dashboard / Jupyter / Playwright stack
SOLO_METHODS = {"solo"}
NON_SOLO_METHODS = {"integration", "live-ib", "e2e"}

# FALLBACK ONLY, for a feature with no declared verification_method. Substring
# matching over templated feature prose fired on 90 of 120 features — "dashboard"
# alone matched 47 — so the override became the normal path rather than the
# exception. Delete this list once every feature carries a method.
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


def _union_deps(base: dict, extra: dict) -> dict:
    """Per-key union of dependency edge-lists (deduped, sorted).

    ``block`` only ever ADDS edges to ``tools/feature_deps.json`` (never removes),
    so reconciling origin/main's committed edges (``base``) with any in-place,
    not-yet-integrated ``block`` edits (``extra``) is a union -- neither side is
    ever dropped. Keys present only in ``base`` keep origin/main's value verbatim.
    """
    merged = {k: list(v) for k, v in base.items()}
    for fid, edges in extra.items():
        merged[fid] = sorted(set(merged.get(fid, [])) | set(edges))
    return merged


def _sync_primary_checkout(root: Path = ROOT) -> None:
    """Fast-forward the primary checkout to origin/main so working-tree reads
    (``load_features`` / ``serialized_notes`` / ``progress.d``) don't lag behind
    features integrated from sibling worktrees -- WITHOUT losing the in-place
    ``block`` edits the canonical ``tools/feature_deps.json`` carries.

    Best-effort and FAIL-SAFE: offline, an ahead/diverged primary checkout, an
    unexpected dirty file, or any git error leaves the checkout untouched. MUST be
    called under ``Lock()`` so no concurrent ``block``/``integrate`` races on
    ``feature_deps.json``. Diagnostics go to STDERR only -- ``cmd_claim``'s stdout
    is ``eval``'d by the launcher and must stay clean.
    """
    git = ["git", "-C", str(root)]
    deps_rel = "tools/feature_deps.json"
    if _run(git + ["fetch", "--quiet", "origin", "main"], check=False).returncode != 0:
        return  # offline / no remote -- best effort
    if (
        _run(git + ["merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD"], check=False).returncode
        != 0
    ):
        return  # primary checkout is ahead of / diverged from origin/main -- leave it for a human
    behind = _run(git + ["rev-list", "--count", "HEAD..FETCH_HEAD"], check=False).stdout.strip()
    if behind in ("", "0"):
        return  # already current
    dirty = [
        line[3:]
        for line in _run(git + ["status", "--porcelain"], check=False).stdout.splitlines()
        if line.strip()
    ]
    unexpected = [p for p in dirty if p != deps_rel]
    if unexpected:
        print(
            "⚠ agent_pool: primary checkout has unexpected local changes "
            f"({', '.join(unexpected)[:200]}); skipping ROOT sync -- it will lag origin/main "
            "until resolved",
            file=sys.stderr,
        )
        return  # never stomp an operator's local edits
    deps_path = root / deps_rel
    local = load_json(deps_path, {})  # snapshot the canonical deps (may hold in-flight block edits)
    _run(git + ["checkout", "--", deps_rel], check=False)  # clean the tree so the ff can apply
    if _run(git + ["merge", "--ff-only", "--quiet", "FETCH_HEAD"], check=False).returncode != 0:
        # Unexpected ff failure -- restore our snapshot and bail (never leave ROOT worse).
        _atomic_write(deps_path, json.dumps(local, indent=2, sort_keys=True) + "\n")
        print("⚠ agent_pool: ROOT fast-forward failed; restored feature_deps.json", file=sys.stderr)
        return
    merged = _union_deps(
        load_json(deps_path, {}), local
    )  # origin/main's edges ∪ our in-place edits
    if merged != load_json(deps_path, {}):
        _atomic_write(deps_path, json.dumps(merged, indent=2, sort_keys=True) + "\n")


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
    """Is this feature NOT solo-verifiable in a parallel session?

    Reads the feature's declared ``verification_method``. The keyword scan below
    is only a fallback for a feature that has not been classified yet: it matched
    templated boilerplate ("dashboard" in 47 features, " ib " in 32) and fired on
    90 of 120 features, which is how ``--force-complete`` became routine rather
    than exceptional. An unrecognised value fails closed (CLAUDE.md rule 3).
    """
    method = str(feat.get("verification_method") or "").strip().lower()
    if method:
        if method in SOLO_METHODS:
            return (False, [])
        if method in NON_SOLO_METHODS:
            return (True, [method])
        return (True, [f"unknown verification_method={method!r}"])

    hay = (" " + feat.get("description", "") + " " + " ".join(feat.get("steps", [])) + " ").lower()
    hits = [kw.strip() for kw in SERIALIZED_KEYWORDS if kw in hay]
    return (bool(hits), hits)


def unclassified(features: list) -> list[str]:
    """Feature ids with no declared verification_method (still on the fallback)."""
    return [f["id"] for f in features if not str(f.get("verification_method") or "").strip()]


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


def _outcome_says_serialized(line: str) -> bool:
    """Is this note's ``Outcome:`` line a real serialized outcome?

    The template's menu line starts with ``complete | ...`` so only a value that
    actually *begins* ``serialized`` counts. One implementation, shared by both
    note sources below, so a working-tree read and an ``origin/main`` read can
    never answer differently.
    """
    s = line.strip().lower()
    if not s.startswith("outcome:"):
        return False
    return s.split("outcome:", 1)[1].strip().startswith("serialized")


def _serialized_from_worktree(progress_dir: Path) -> set:
    """Serialized ids from notes on disk (the primary checkout's working tree)."""
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
            if line.strip().lower().startswith("outcome:"):
                if _outcome_says_serialized(line):
                    out.add(fid)
                break  # only the FIRST Outcome: line is the outcome
    return out


def _serialized_from_ref(ref: str) -> set | None:
    """Serialized ids from the notes as COMMITTED on ``ref`` (normally origin/main).

    Returns ``None`` when the ref cannot be read (offline first run / no remote),
    so the caller can fall back rather than silently reporting "no serialized
    notes" — which would re-offer every already-done feature.

    One ``git grep`` for every ``Outcome:`` line across the committed notes; the
    LOWEST line number per file is that note's outcome, matching the working-tree
    reader's first-line-wins rule.
    """
    proc = _run(
        [
            "git",
            "-C",
            str(ROOT),
            "grep",
            "-n",
            "-i",
            "-E",
            "^[[:space:]]*outcome:",
            ref,
            "--",
            "progress.d/session-*.md",
        ],
        check=False,
    )
    if proc.returncode not in (0, 1):  # 1 == no matches, anything else == unreadable ref
        return None
    first: dict[str, tuple[int, str]] = {}
    for raw in proc.stdout.splitlines():
        parts = raw.split(":", 3)  # <ref>:<path>:<lineno>:<content>
        if len(parts) != 4:
            continue
        _rev, path, lineno, content = parts
        if not lineno.isdigit():
            continue
        name = Path(path).name
        if not (name.startswith("session-") and name.endswith(".md")):
            continue
        fid = name[len("session-") : -len(".md")]
        n = int(lineno)
        if fid not in first or n < first[fid][0]:
            first[fid] = (n, content)
    return {fid for fid, (_n, content) in first.items() if _outcome_says_serialized(content)}


def serialized_notes(progress_dir: Path | None = None) -> set:
    """Feature ids whose ``progress.d/session-<id>.md`` records ``Outcome:
    serialized`` — code is done but ≥1 step needs human IB/e2e verification.

    Re-offering such a feature to a fresh agent is the churn loop: it can only
    ever integrate ``serialized`` again (never ``complete``), so it returns to
    the ready frontier forever. We exclude these from claiming and surface them
    as an ``awaiting_verification`` bucket for the operator to close by hand.

    Read from ``origin/main`` UNION the primary checkout's working tree. The
    working tree alone is not enough: notes reach ROOT only when it advances, and
    ``_sync_primary_checkout`` is fast-forward-only, so it correctly refuses
    exactly when the operator is doing their own work in ROOT (a WIP branch, a
    dirty tree) — which is the normal case. On 2026-07-27 that left the four
    newest notes invisible and two agents were simultaneously re-offered
    already-serialized features. The union can only ever ADD ids to the
    awaiting bucket, so it strictly reduces churn.

    ``progress_dir`` overrides both sources with one directory (tests).
    """
    if progress_dir is not None:
        return _serialized_from_worktree(progress_dir)
    local = _serialized_from_worktree(ROOT / "progress.d")
    integrated = _serialized_from_ref(base_ref())
    return local if integrated is None else (local | integrated)


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


def worktree_for_branch(branch: str) -> Path | None:
    """Path of the worktree that already has ``branch`` checked out, if any.

    Git refuses to check one branch out in two worktrees, so an operator-selected
    ``--branch`` must reuse the existing checkout rather than try to add a second.
    """
    out = _run(["git", "-C", str(ROOT), "worktree", "list", "--porcelain"]).stdout
    current = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree ") :]).resolve()
        elif line == f"branch refs/heads/{branch}" and current is not None:
            return current
    return None


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
                    "awaiting_without_dep_edges": [f for f in awaiting if not deps.get(f)],
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

    # How much of `done` is actually backed by evidence. A feature closed before
    # the evidence gate existed is not re-verified and must not read as if it were.
    passing = [f for f in by_id.values() if f.get("passes")]
    pre_gate = [f["id"] for f in passing if f.get("evidence") == evidence.PRE_GATE]
    # A gated close RETIRES evidence.json to closed-<ts>.json, so counting the live
    # file counted only the closes where retirement FAILED — the counter was inverted.
    evidenced = [
        f["id"]
        for f in passing
        if (evidence.RUNS_DIR / f["id"]).is_dir()
        and any((evidence.RUNS_DIR / f["id"]).glob("*.json"))
    ]
    if pre_gate or evidenced:
        print(
            f"   evidence: {len(evidenced)} evidenced, {len(pre_gate)} pre-gate "
            f"(closed before the gate existed; NOT re-verified)"
        )
    unclass = unclassified(list(by_id.values()))
    if unclass:
        print(
            f"   ⚠ {len(unclass)} feature(s) have no verification_method — the honesty "
            f"guard is falling back to keyword matching for them "
            f"(fix: tools/classify_verification.py propose)"
        )

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
        edgeless = []
        for fid in awaiting:
            if deps.get(fid):
                print(f"  {fid:18} {by_id[fid]['description'][:52]}")
            else:
                edgeless.append(fid)
                print(f"  {fid:18} {by_id[fid]['description'][:44]}  ⚠ no dep edges")
        if edgeless:
            # A note is the only thing keeping these off the frontier, and a note
            # is only as good as the reader's view of it. A `block --on` edge is
            # branch-independent and immediate -- it is the durable de-churn.
            print(
                "   ⚠ these are de-churned by their note ALONE. If a real unbuilt producer\n"
                "     blocks the flip, record it: agent_pool.py block <id> --on <owner ids>\n"
                f"     (derive the owners from the code, not from prose): {', '.join(edgeless[:5])}"
            )
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


def _finish_claim(runtime: dict, active: dict, fid: str, branch: str, wt: Path, owner: str) -> int:
    """Take the lease, materialise the worktree, print the shell env. Call under the lock.

    Shared by the auto-pick and ``--id`` paths so both allocate ports from the
    same free-index pool and create branches under the same mutex.
    """
    idx = free_port_index(active)
    runtime["leases"][fid] = {
        "owner": owner,
        "ts": int(time.time()),
        "expiry": int(time.time() + LEASE_TTL),
        "port_index": idx,
    }

    # Create (or reuse) the worktree + branch under the lock so two claimers
    # never race on branch creation.
    if not wt.exists():
        if branch_exists(branch):
            _run(["git", "-C", str(ROOT), "worktree", "add", str(wt), branch])
        else:
            _run(["git", "-C", str(ROOT), "worktree", "add", "-b", branch, str(wt), base_ref()])

    save_runtime(runtime)

    print(f"FEATURE={fid}")
    print(f"WORKTREE={wt}")
    print(f"BRANCH={branch}")
    for k, v in ports_for(idx).items():
        print(f"{k}={v}")
    return 0


def cmd_claim(args):
    if getattr(args, "branch", None) and not getattr(args, "id", None):
        print("✗ --branch requires --id (it pins ONE feature's session).", file=sys.stderr)
        return 1
    with Lock():
        # Keep the primary checkout current with origin/main FIRST (under the lock,
        # so no block/integrate races) -- otherwise serialized_notes()/load_features()
        # read a stale working tree and re-offer already-de-churned features.
        _sync_primary_checkout()
        features = load_features(fetch=True)
        deps = load_deps()
        runtime = load_runtime()
        ready, blocked, active, held, by_id = compute(
            features, deps, runtime, allow_foreign_reclaim=args.reclaim
        )

        owner = os.environ.get("ATP_AGENT_OWNER") or f"{socket.gethostname()}:{os.getpid()}"

        # --- operator-selected target -------------------------------------
        # `--id` deliberately bypasses the ready-frontier filter AND the
        # awaiting-verification skip: those guards exist to stop an autonomous
        # agent from churning on a flip-blocked feature, but a human closing one
        # out by hand is exactly the case they should not block. Unmet deps are
        # reported, not enforced.
        if getattr(args, "id", None):
            fid = args.id
            if fid not in by_id:
                hint = difflib.get_close_matches(fid, by_id, n=2)
                print(
                    f"✗ unknown feature id: {fid}" + (f" (did you mean {hint}?)" if hint else ""),
                    file=sys.stderr,
                )
                return 1
            if by_id[fid].get("passes") is True:
                print(f"✗ {fid} already passes on origin/main — nothing to close.", file=sys.stderr)
                return 1
            lease = active.get(fid)
            if lease and lease.get("owner") != owner and not args.reclaim:
                print(
                    f"✗ {fid} is leased by {lease.get('owner')} (another live session).\n"
                    f"  Re-run with --reclaim to take it, or `release {fid} --force` first.",
                    file=sys.stderr,
                )
                return 1

            branch = args.branch or f"agent/{fid}"
            if args.branch:
                # An operator-pinned branch may already be checked out somewhere
                # (git allows only one worktree per branch) — reuse that path.
                wt = worktree_for_branch(branch) or (
                    ROOT.parent / f"alphalabs-wt-{branch.split('/')[-1]}"
                )
            else:
                wt = ROOT.parent / f"alphalabs-wt-{fid}"
            if worktree_dirty(wt) and not args.reclaim:
                print(
                    f"✗ {wt} has uncommitted changes — refusing to reuse it.\n"
                    f"  Commit/stash there, or re-run with --reclaim.",
                    file=sys.stderr,
                )
                return 1

            unmet = blocked.get(fid) or []
            if unmet:
                print(
                    f"# note: {fid} has unmet deps ({', '.join(sorted(unmet))}) — "
                    "claiming anyway (operator-selected).",
                    file=sys.stderr,
                )
            return _finish_claim(runtime, active, fid, branch, wt, owner)

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

        return _finish_claim(
            runtime,
            active,
            choice,
            f"agent/{choice}",
            ROOT.parent / f"alphalabs-wt-{choice}",
            owner,
        )


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


def cmd_unblock(args):
    """Retract a dependency edge that turned out to be wrong.

    `block --on` appends and nothing has ever removed an edge, so the graph only
    accretes constraints. A wrong edge is not inert: the scheduler will not offer the
    feature until the named prerequisite passes, so one bad `block` can park a
    feature indefinitely. `status` already surfaces the symptom (⚠ no dep edges); this
    is the correction. Same lock, same validation, same file as `block`.
    """
    fid = args.id
    with Lock():
        ids = {f["id"] for f in load_features(fetch=False)}
        if fid not in ids:
            print(f"✗ unknown feature id: {fid}", file=sys.stderr)
            return 1
        deps = load_deps()
        cur = set(deps.get(fid, []))
        if not cur:
            print(f"• {fid} has no recorded dependencies; nothing to retract")
            return 0
        wanted = set(args.off)
        missing = sorted(wanted - cur)
        if missing:
            print(
                f"✗ {fid} is not blocked on {missing} (current: {sorted(cur)})",
                file=sys.stderr,
            )
            return 1
        cur -= wanted
        if cur:
            deps[fid] = sorted(cur)
        else:
            deps.pop(fid, None)
        save_deps(deps)
    reason = args.reason or "(no reason given)"
    print(f"✓ {fid} no longer blocked on {sorted(wanted)} — {reason}")
    print(f"  remaining: {sorted(cur) if cur else 'none'}")
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


def staged_outside_allowlist(names: list[str], fid: str | None = None) -> list[str]:
    """Staged path names that fall outside what the integrator may commit.

    That is the allowlist, plus — when integrating feature ``fid`` — that one
    feature's evidence directory, so close_feature's retirement of
    ``.harness/runs/<fid>/evidence.json`` can actually reach main. Scoped to a single
    id on purpose: no branch may stage another feature's evidence, and the ledgers
    are not in a worktree at all.
    """
    own_evidence = f".harness/runs/{fid}/" if fid else None
    return [
        p
        for p in names
        if p and not path_in_allowlist(p) and not (own_evidence and p.startswith(own_evidence))
    ]


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
    own_evidence = f".harness/runs/{fid}/"
    permanent = {"progress.d/README.md", "progress.d/.gitkeep"}
    bad = []
    for p in committed_paths:
        if p in ("feature_list.json", "progress.txt", "tools/feature_deps.json"):
            bad.append(p)
        elif p.startswith("progress.d/") and p != own_note and p not in permanent:
            bad.append(p)
        # An agent commits its OWN evidence with its branch; anything else under
        # .harness from a branch is refused. The ledgers are not in the allowlist and
        # live in the primary checkout, so they cannot be reached from here at all —
        # this is the second line of defence, not the only one.
        elif p.startswith(".harness/") and not p.startswith(own_evidence):
            bad.append(p)
    return bad


def note_rounds_mismatch(wt: Path, fid: str) -> str | None:
    """Does the session note's `Adversarial rounds:` match what the reviewer recorded?

    The field is the only falsifiable claim the playbook system makes, and it
    appeared in 1 of 38 notes because it was prose an agent had to remember. Now that
    adversarial_review.py records each round, the note can be checked against it.
    Returns a message when they disagree, else None. Absent telemetry is NOT a
    mismatch — a review can legitimately predate this, or run outside the worktree.
    """
    jsonl = wt / ".harness" / "runs" / fid / "review.jsonl"
    note = wt / "progress.d" / f"session-{fid}.md"
    if not jsonl.is_file() or not note.is_file():
        return None
    try:
        recorded = sum(1 for ln in jsonl.read_text(encoding="utf-8").splitlines() if ln.strip())
        text = note.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not recorded:
        return None
    m = re.search(r"^adversarial rounds:\s*(\d+)", text, re.MULTILINE | re.IGNORECASE)
    if not m:
        return (
            f"session note has no `Adversarial rounds:` line, but the reviewer "
            f"recorded {recorded} round(s) in .harness/runs/{fid}/review.jsonl"
        )
    claimed = int(m.group(1))
    if claimed != recorded:
        return (
            f"session note claims `Adversarial rounds: {claimed}` but the reviewer "
            f"recorded {recorded} in .harness/runs/{fid}/review.jsonl"
        )
    return None


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

    # Honesty guard (code-enforced backstop): refuse `complete` for a feature whose
    # declared verification_method (or, absent one, whose text) implies IB /
    # integration / live / e2e steps unless forced.
    feat = next((x for x in load_features(fetch=False) if x["id"] == fid), None)
    if mode == "complete" and not args.force_complete:
        if feat:
            need, hits = needs_serialized(feat)
            if need:
                print(
                    f"✗ {fid}: verification method {hits} is not solo-verifiable → "
                    f"use --mode serialized (or --force-complete if you genuinely "
                    f"verified every step solo).",
                    file=sys.stderr,
                )
                return 5
    # The evidence gate. `complete` used to hand close_feature.py the --verified flag
    # that flag's own error text reserves for a human; the agent attested to its own
    # work. Now an incomplete record DEGRADES the integrate to `serialized`: the code
    # still lands on main, `passes` stays false, and the note becomes the resume
    # pointer the operator picks up. The honest outcome is the automatic one.
    #
    # A corrupt or unreadable record must reach that same degrade, not a traceback:
    # evidence.verify raises EvidenceError by design (absent != empty), and an
    # unhandled one out of `integrate` is the fail-open this gate exists to prevent.
    #
    # --force-complete overrides the HONESTY GUARD above (the declared
    # verification_method), and nothing else. It is a flag on the agent's own CLI —
    # the playbooks tell agents to reach for it — so letting it also skip the evidence
    # gate, or pass `--attested-by operator` to close_feature.py, would let the agent
    # self-grant a HUMAN's attestation: the original self-granted `--verified` defect
    # renamed, and worse, because .harness/closes.jsonl would then record a human
    # attestation that no human made. A real human override runs close_feature.py
    # directly with --attested-by. Captured here only so the ledger entry survives the
    # degrade rewriting `mode` below.
    forced_complete = mode == "complete" and args.force_complete
    if mode == "complete":
        try:
            ok, problems, summary = evidence.verify(fid)
        except evidence.EvidenceError as exc:
            ok, problems = False, [f"evidence record unreadable: {exc}"]
            summary = {"steps_evidenced": 0, "steps_total": "?"}
        # RE-EXECUTION. The record is written by the party being audited; `run` makes
        # the honest path easy but `executed: true` is still a boolean in a file the
        # agent's branch controls. For a feature whose own declared method says every
        # step is verifiable solo, the integrator re-runs the recorded commands here
        # and compares exit codes — so the pass the close depends on is produced by
        # the integrator, not asserted by the agent.
        #
        # Deliberately BEFORE `with Lock():` — it is read-only w.r.t. shared state,
        # and a full re-verification must not block siblings. Only solo features:
        # everything else already requires a named human attestation, which
        # re-execution cannot supply and does not improve.
        if ok and feat and not needs_serialized(feat)[0]:
            re_ok, re_problems = evidence.reexecute(fid, wt)
            if not re_ok:
                ok = False
                problems = re_problems
                summary = dict(summary, reexecuted=False)

        if not ok:
            print(
                f"⚠ {fid}: evidence record is incomplete "
                f"({summary['steps_evidenced']}/{summary['steps_total']} steps) — "
                f"integrating as `serialized` instead of `complete`:",
                file=sys.stderr,
            )
            for p in problems[:8]:
                print(f"    · {p}", file=sys.stderr)
            if len(problems) > 8:
                print(f"    · … and {len(problems) - 8} more", file=sys.stderr)
            print(
                f"  The code will merge and `passes` stays false. To close it properly, "
                f"EXECUTE each step through the recorder — `record` only writes what you "
                f"tell it and does not satisfy this gate on its own:\n"
                f"    python3 tools/evidence.py run {fid} --step N -- <the command>\n"
                f"  commit .harness/runs/{fid}/evidence.json with your feature work, then "
                f"re-run `integrate {fid} --mode complete`.",
                file=sys.stderr,
            )
            mode = "serialized"

    if args.dry_run:
        ahead = _run(
            ["git", "-C", str(wt), "rev-list", "--count", f"{base_ref()}..HEAD"], check=False
        ).stdout.strip()
        print(
            f"[dry-run] {fid} mode={mode}: {ahead} commit(s) ahead of {base_ref()}; "
            f"would rebase, {'flip passes:true + ' if mode == 'complete' else ''}push HEAD:main, release lease"
        )
        return 0

    # Audit the override only once we are past every early return. Logging it above
    # meant `--dry-run --force-complete` permanently recorded an override for an
    # integration that never happened — a write on the one path whose entire contract
    # is that it writes nothing, corrupting the trail this gate exists to create.
    # `forced_complete` is captured BEFORE the evidence gate can rewrite `mode`.
    # Keying this on `mode == "complete"` meant a force-complete with no evidence —
    # precisely the event this ledger exists to capture, and which AGENTS.md promises
    # is always recorded — degraded to serialized and was never logged at all.
    if forced_complete:
        evidence.log_override(
            fid, "force-complete", getattr(args, "reason", "") or "(no reason given)"
        )

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
            # The recorded rounds and the note must agree. Not cosmetic: the round
            # count is the falsifiable claim the playbook loop is measured by, and a
            # note that disagrees with the reviewer's own log makes every later
            # "are the playbooks working?" answer unreliable.
            if (mismatch := note_rounds_mismatch(wt, fid)) is not None:
                print(
                    f"✗ {fid}: {mismatch}.\n  Fix the note to match, then re-run integrate.",
                    file=sys.stderr,
                )
                return 12

            violations = shared_state_violations(_branch_committed(wt), fid)
            if violations:
                print(
                    f"✗ {fid}: branch commits modify shared coordination files {violations} — "
                    f"only the integrator may write them. Revert those commits.",
                    file=sys.stderr,
                )
                return 6

            # THE TRUST BOUNDARY. Everything the integrator owns is reset to the base
            # ref before the integrator writes it, so nothing the agent left in its
            # worktree can ride along.
            #
            # shared_state_violations() catches these only when COMMITTED. Uncommitted
            # edits passed every check — they are inside INTEGRATE_ALLOWLIST, so
            # _uncommitted_outside_allowlist ignored them and `git add -A` staged them.
            # That made the human gate self-grantable end to end: an agent could hand-
            # write evidence, run `close_feature.py --verified --attested-by operator`
            # in its own worktree, then `integrate --mode serialized` and have the
            # mutated feature_list.json pushed to main with passes:true.
            _run(
                ["git", "-C", str(wt), "checkout", base_ref(), "--", *INTEGRATOR_OWNED],
                check=False,
            )

            # Recompute the flip against the just-rebased (latest main) tree, so the
            # close commit is fresh each attempt — never rebased, so a concurrent
            # flip on main can't conflict on the whole-file feature_list rewrite.
            _sync_deps_into(wt)
            if mode == "complete":
                # No --attested-by here, ever. agent_pool is the agent's own CLI; an
                # attestation it can grant itself is not an attestation, and writing
                # one into .harness/closes.jsonl would put a human's name on a claim
                # no human made. A real override is a human running close_feature.py
                # directly with --attested-by operator.
                #
                # check=False: close_feature re-verifies AFTER the rebase, so it can
                # legitimately refuse (exit 3) on a record this function accepted
                # before it — e.g. a concurrently re-specified steps[] changing the
                # digest. An uncaught CalledProcessError here would escape `with
                # Lock():` with the branch already rebased.
                close = _run(
                    [sys.executable, str(wt / "tools" / "close_feature.py"), fid, "--verified"],
                    check=False,
                )
                if close.returncode != 0:
                    print(
                        f"✗ {fid}: close_feature refused after the rebase "
                        f"(exit {close.returncode}):\n{close.stderr.strip()}\n"
                        f"  Nothing was pushed; the branch is rebased and intact. "
                        f"Re-record the affected steps and re-run integrate.",
                        file=sys.stderr,
                    )
                    return 11

            # Stage ONLY the integration allowlist (never `git add -A`), so the
            # marker commit can never contain feature work.
            existing = [p for p in INTEGRATE_ALLOWLIST if (wt / p).exists()]
            # …plus THIS feature's evidence directory, so close_feature's retirement
            # (evidence.json -> closed-<ts>.json) actually reaches main. Renaming it
            # in the worktree without staging it left the live, verifying record on
            # origin/main forever, where every later worktree inherits it — the whole
            # point of retiring it. Scoped to one feature id, never all of .harness:
            # the ledgers live outside the worktree and no branch may stage another
            # feature's evidence.
            ev_dir = f".harness/runs/{fid}"
            if (wt / ev_dir).exists():
                existing.append(ev_dir)
            if existing:
                _run(["git", "-C", str(wt), "add", "-A", "--", *existing])
            # Final assertion before committing: nothing outside the allowlist may
            # be staged (e.g. a pre-staged rename source riding in the index).
            outside = staged_outside_allowlist(_staged_paths(wt), fid)
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

    # Garden the feature we just closed. cleanup_agents.sh has existed since the
    # spawn-agents era with --dry-run, a dirty-tree refusal and the correct
    # passes:true signal — and had never run, because nothing triggered it: 49
    # worktrees, 24 merged branches with no worktree, and 3 plan files for closed
    # features had accumulated. A garbage collector with no trigger is not one.
    #
    # Scoped to this feature id, outside the lock (it only touches this feature's
    # own worktree and branch), and never fatal: a failed cleanup must not turn a
    # successful integrate into a failure.
    if mode == "complete":
        script = ROOT / "tools" / "cleanup_agents.sh"
        if script.is_file():
            gc = _run([str(script), fid], check=False)
            if gc.returncode != 0:
                print(
                    f"⚠ {fid}: integrated, but cleanup_agents.sh exited "
                    f"{gc.returncode}; run it by hand.",
                    file=sys.stderr,
                )
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
    cp.add_argument(
        "--id",
        metavar="FEATURE_ID",
        help="claim THIS feature instead of auto-picking (operator-selected). "
        "Bypasses the ready-frontier and awaiting-verification filters; "
        "unmet deps are reported, not enforced.",
    )
    cp.add_argument(
        "--branch",
        metavar="BRANCH",
        help="with --id: bind the session to an existing branch (e.g. "
        "agent/SRS-MD-003-stream) instead of agent/<ID>. Reuses that branch's "
        "existing worktree if it already has one.",
    )

    bp = sub.add_parser("block", help="record discovered dependency edge(s) + release lease")
    bp.add_argument("id")
    bp.add_argument("--on", nargs="+", required=True, metavar="DEP_ID")
    bp.add_argument("--reason", default="")

    up = sub.add_parser("unblock", help="retract dependency edge(s) recorded by `block`")
    up.add_argument("id")
    up.add_argument("--off", nargs="+", required=True, metavar="DEP_ID")
    up.add_argument("--reason", default="")

    ip = sub.add_parser("integrate", help="rebase+merge to main; flip passes on complete")
    ip.add_argument("id")
    ip.add_argument("--mode", choices=["complete", "partial", "serialized"], required=True)
    ip.add_argument(
        "--force-complete",
        action="store_true",
        help="override the IB/integration honesty guard (you verified every step solo)",
    )
    ip.add_argument(
        "--reason",
        default="",
        help="why --force-complete was needed; recorded in .harness/overrides.jsonl. "
        "log_override read args.reason via getattr() while this flag did not exist, "
        "so every override entry was permanently '(no reason given)'.",
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
        "unblock": cmd_unblock,
        "integrate": cmd_integrate,
        "heartbeat": cmd_heartbeat,
        "release": cmd_release,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
