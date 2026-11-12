#!/usr/bin/env python3
"""Typed dependencies and the four status dimensions of a feature.

Implementation completion and requirement verification are SEPARATE lifecycle
events. Before this module the scheduler had one signal - ``passes`` - and
blocked a feature until every prerequisite had ``passes: true``. ``passes``
means *requirement verified* (a live IB window, an approving reviewer, sometimes
a human attestation), so one unverified upstream froze every downstream claim:
37 of 50 open MVP features had merged code while 41 showed as "blocked".

Every edge ``A -> B`` in ``tools/feature_deps.json`` now has a TYPE, recorded in
``tools/dependency_types.json``:

``implementation``  A's code cannot be written until B's implementation is
                    complete. Gates A's implementation.
``interface``       A needs only B's contract (a type, port, schema, or route).
                    Gates A's implementation until the contract EXISTS in the
                    tree; B need not be finished.
``integration``     A's code stands alone; proving A's integration obligations
                    needs B running. Gates A's integration verification only.
``verification``    A's requirement evidence relies on B's. Gates nothing
                    before A's requirement close.

An edge with no classification is ``unclassified`` and is treated exactly like
``implementation`` - unknown is never empty (CLAUDE.md rule 3). Classifying an
edge can only ever relax WHEN work may start, never WHAT a close requires:
``close_gate`` still demands every prerequisite of every type be ``passes:
true`` before a requirement closes, which is the old claim-time rule moved to
the moment it actually protects.

Pure functions only. ``agent_pool.py`` owns IO, locking, and git.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

EDGE_TYPES = ("implementation", "interface", "integration", "verification")
UNCLASSIFIED = "unclassified"
# Edge types that gate a claim for implementation work.
IMPLEMENTATION_GATING = frozenset({"implementation", "interface", UNCLASSIFIED})
CONFIDENCE = ("high", "medium", "low")
TYPES_REL = "tools/dependency_types.json"

# The four lifecycle dimensions. Adapted to this repo: `passes` IS the
# requirement-verified flag, and the evidence record IS the verification store,
# so nothing here duplicates a field that already exists.
IMPL_STATES = ("not_started", "ready", "in_progress", "blocked", "complete")
TEST_STATES = ("not_tested", "passed", "failed", "blocked")
SYSTEM_STATES = ("not_verified", "partially_verified", "verified", "failed", "blocked")

PRE_GATE = "pre-gate"


class DependencyModelError(Exception):
    """The dependency model's own data is unreadable or malformed."""


# ----------------------------------------------------------------------------
# Edge classification
# ----------------------------------------------------------------------------
def edge_key(a: str, b: str) -> str:
    """The dependency identifier: ``"A -> B"`` means A depends on B."""
    return f"{a} -> {b}"


def parse_edge_key(key: str) -> tuple[str, str] | None:
    parts = key.split(" -> ")
    if len(parts) != 2 or not all(p.strip() for p in parts):
        return None
    return parts[0].strip(), parts[1].strip()


def load_types(path: Path) -> dict:
    """The classification file. Missing means every edge is unclassified.

    A file that exists but cannot be parsed raises: reading it as empty would make
    every edge unclassified, which happens to fail closed, but it would also hide a
    broken file behind a board that merely looks more blocked than usual.
    """
    if not path.exists():
        return {"edges": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DependencyModelError(f"{path.name} is corrupt: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("edges", {}), dict):
        raise DependencyModelError(f"{path.name}: expected an object with an 'edges' map")
    data.setdefault("edges", {})
    return data


def classification(types: dict, a: str, b: str) -> dict:
    """The edge's EFFECTIVE classification for gating.

    A low-confidence classification is a guess an operator has not confirmed, so
    it gates exactly like no classification at all (CLAUDE.md rule 3). The guess
    is kept under ``low_confidence_of`` so `status` can say what was suspected.
    """
    meta = (types.get("edges") or {}).get(edge_key(a, b))
    if not isinstance(meta, dict) or meta.get("type") not in EDGE_TYPES:
        return {"type": UNCLASSIFIED}
    if meta.get("confidence") == "low":
        return {
            "type": UNCLASSIFIED,
            "low_confidence_of": meta.get("type"),
            "reason": meta.get("reason", ""),
        }
    return meta


def edge_type(types: dict, a: str, b: str) -> str:
    return classification(types, a, b)["type"]


def validate_types(types: dict, deps: dict) -> list[str]:
    """Every problem with the classification file, against the edge set it annotates."""
    problems: list[str] = []
    edges = {(a, b) for a, prereqs in deps.items() for b in prereqs}
    for key, meta in sorted((types.get("edges") or {}).items()):
        parsed = parse_edge_key(key)
        if parsed is None:
            problems.append(f"{key!r}: not an 'A -> B' dependency identifier")
            continue
        if parsed not in edges:
            problems.append(
                f"{key}: classified but absent from tools/feature_deps.json "
                "(a classification must annotate a real edge; remove it or restore the edge)"
            )
        if not isinstance(meta, dict):
            problems.append(f"{key}: classification must be an object")
            continue
        if meta.get("type") not in EDGE_TYPES:
            problems.append(f"{key}: type {meta.get('type')!r} is not one of {EDGE_TYPES}")
        for field in ("reason", "source"):
            if not str(meta.get(field) or "").strip():
                problems.append(f"{key}: missing '{field}' (say why the edge exists, and where)")
        if meta.get("confidence", "high") not in CONFIDENCE:
            problems.append(f"{key}: confidence must be one of {CONFIDENCE}")
        if meta.get("type") == "interface":
            contract = meta.get("contract")
            if not isinstance(contract, dict) or not str(contract.get("path") or "").strip():
                problems.append(
                    f"{key}: an interface edge needs contract.path (the file that defines it)"
                )
            elif contract.get("pattern"):
                try:
                    re.compile(str(contract["pattern"]))
                except re.error as exc:
                    problems.append(f"{key}: contract.pattern is not a valid regex: {exc}")
    return problems


# ----------------------------------------------------------------------------
# Feature predicates
# ----------------------------------------------------------------------------
def steps_digest(steps: list) -> str:
    """Same fingerprint as tools/evidence.py steps_digest (kept import-free here)."""
    return hashlib.sha256("\n".join(str(s) for s in steps).encode("utf-8")).hexdigest()[:16]


def implementation_complete(feat: dict) -> bool:
    """Implementation is complete when it was recorded so, or the requirement passed.

    A verified requirement implies its implementation exists; the converse never
    holds, which is the whole point of the split. A recorded completion carries the
    digest of the steps it was judged against: re-specifying the feature voids it,
    exactly as a re-spec voids an evidence record.
    """
    if feat.get("passes") is True:
        return True
    impl = feat.get("implementation")
    if not (isinstance(impl, dict) and impl.get("status") == "complete"):
        return False
    # No digest means nobody can tell what the record was judged against: not complete.
    recorded = impl.get("steps_digest")
    return recorded is not None and recorded == steps_digest(feat.get("steps", []))


def implementation_partial(feat: dict) -> bool:
    impl = feat.get("implementation")
    return isinstance(impl, dict) and impl.get("status") == "partial"


def interface_satisfied(meta: dict, root: Path) -> tuple[bool, str]:
    """Does the contract an interface edge names exist in the tree?

    ``contract.path`` must be a file; ``contract.symbol``, when given, must appear
    in it. A contract that names a file nobody has written, or a field nobody has
    added, is not "sufficiently defined" and keeps the consumer blocked.
    """
    contract = meta.get("contract") or {}
    rel = str(contract.get("path") or "").strip()
    if not rel:
        return False, "interface edge names no contract path"
    path = (root / rel).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False, f"contract path {rel} is outside the repository"
    if not path.is_file():
        return False, f"contract {rel} does not exist yet"
    symbol = str(contract.get("symbol") or "").strip()
    pattern = str(contract.get("pattern") or "").strip()
    if symbol or pattern:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"contract {rel} unreadable: {exc}"
        if symbol and symbol not in text:
            return False, f"contract {rel} does not define {symbol!r} yet"
        # A name can appear in a file without being part of the contract (a field
        # mentioned in a docstring is not a field in the schema). `pattern` pins the
        # exact shape: a regex that must match, DOTALL so it can span a tuple.
        if pattern:
            try:
                found = re.search(pattern, text, re.DOTALL)
            except re.error as exc:
                return False, f"contract pattern for {rel} is not a valid regex: {exc}"
            if not found:
                return False, f"contract {rel} does not yet match {pattern!r}"
    label = symbol or (f"/{pattern}/" if pattern else "")
    return True, f"contract {rel}{'::' + label if label else ''} exists"


# ----------------------------------------------------------------------------
# Gates
# ----------------------------------------------------------------------------
def implementation_blockers(
    fid: str, deps: dict, types: dict, by_id: dict, root: Path
) -> list[dict]:
    """The edges that stop ``fid``'s implementation from starting, with reasons."""
    out: list[dict] = []
    for dep in deps.get(fid, []):
        if dep not in by_id:
            continue
        meta = classification(types, fid, dep)
        kind = meta["type"]
        if kind in ("implementation", UNCLASSIFIED):
            if not implementation_complete(by_id[dep]):
                why = f"needs {dep}'s implementation complete"
                if meta.get("low_confidence_of"):
                    why += (
                        f" (classified {meta['low_confidence_of']} with LOW confidence - gates "
                        "until an operator confirms it)"
                    )
                elif kind == UNCLASSIFIED:
                    why += " (edge unclassified: treated as an implementation dependency)"
                out.append({"dep": dep, "type": kind, "reason": why})
        elif kind == "interface":
            ok, why = interface_satisfied(meta, root)
            if not ok:
                out.append({"dep": dep, "type": kind, "reason": why})
    return out


def integration_blockers(fid: str, deps: dict, types: dict, by_id: dict) -> list[dict]:
    """Integration edges whose peer has no complete implementation to test against."""
    return [
        {"dep": dep, "type": "integration", "reason": f"needs {dep} implemented to test against"}
        for dep in deps.get(fid, [])
        if dep in by_id
        and edge_type(types, fid, dep) == "integration"
        and not implementation_complete(by_id[dep])
    ]


def close_gate(fid: str, deps: dict, by_id: dict) -> list[str]:
    """Prerequisites (of ANY type) that are not yet requirement-verified.

    The requirement close keeps the full strictness the claim-time rule used to
    impose. Moving the rule here is what lets implementation proceed without
    letting any requirement close on less than it did before.
    """
    return sorted(d for d in deps.get(fid, []) if d in by_id and by_id[d].get("passes") is not True)


# ----------------------------------------------------------------------------
# Cycles
# ----------------------------------------------------------------------------
def strongly_connected(succ: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan's SCCs, iterative. Returns components of size > 1, plus self-loops."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    nodes = set(succ) | {w for ws in succ.values() for w in ws}
    for start in sorted(nodes):
        if start in index:
            continue
        work = [(start, iter(sorted(succ.get(start, []))))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(succ.get(w, [])))))
                    advanced = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1 or v in succ.get(v, []):
                    out.append(sorted(comp))
    return sorted(out)


def cycle_report(deps: dict, types: dict, by_id: dict) -> dict:
    """Split cycles into true implementation deadlocks and deferred ones.

    An implementation deadlock is a cycle of implementation-gating edges whose
    targets are not yet implemented: no member can ever start. Every other cycle
    (it passes through an integration or verification edge, or a member is already
    implemented) lets implementation proceed but would deadlock the requirement
    close, because ``close_gate`` needs every prerequisite verified - so it is
    reported for an operator decision about which edge is real.
    """
    gating: dict[str, list[str]] = {}
    full: dict[str, list[str]] = {}
    for a, prereqs in deps.items():
        for b in prereqs:
            if a not in by_id or b not in by_id:
                continue
            full.setdefault(a, []).append(b)
            if edge_type(types, a, b) in IMPLEMENTATION_GATING and not implementation_complete(
                by_id[b]
            ):
                gating.setdefault(a, []).append(b)
    implementation = strongly_connected(gating)
    impl_sets = [set(c) for c in implementation]
    deferred = [c for c in strongly_connected(full) if not any(set(c) <= s for s in impl_sets)]
    return {"implementation": implementation, "deferred": deferred}


# ----------------------------------------------------------------------------
# Retry guard
# ----------------------------------------------------------------------------
def fingerprint(
    fid: str,
    by_id: dict,
    deps: dict,
    types: dict,
    root: Path,
    *,
    contract_text: str = "",
    record_text: str = "",
) -> str:
    """A digest of everything that could unblock ``fid``.

    A parked feature is re-offered only when this changes: a prerequisite's status,
    an edge's type, a contract appearing, the feature's own spec, its contract, or
    its evidence. Re-trying with nothing changed is the churn loop.
    """
    feat = by_id.get(fid, {})
    state = {
        "feature": {
            k: feat.get(k)
            for k in (
                "steps",
                "passes",
                "implementation",
                "external_blocker",
                "needs_clarification",
                "verification_method",
            )
        },
        "deps": [
            {
                "dep": d,
                "type": edge_type(types, fid, d),
                "implemented": implementation_complete(by_id[d]) if d in by_id else None,
                "passes": by_id[d].get("passes") if d in by_id else None,
                "interface": (
                    interface_satisfied(classification(types, fid, d), root)[0]
                    if edge_type(types, fid, d) == "interface"
                    else None
                ),
            }
            for d in sorted(deps.get(fid, []))
        ],
        "contract": hashlib.sha256(contract_text.encode()).hexdigest(),
        "record": hashlib.sha256(record_text.encode()).hexdigest(),
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------
# Status dimensions
# ----------------------------------------------------------------------------
def obligation_runs(record: dict | None, oid: str) -> list[dict]:
    return list(((record or {}).get("obligations") or {}).get(oid, {}).get("runs") or [])


def level_status(
    record: dict | None,
    contract: dict | None,
    level: str,
    *,
    blockers: list[dict] | None = None,
    code_changed_since=None,
) -> dict:
    """Component or integration verification state, from executed obligation runs.

    The LATEST executed run decides each obligation, and every earlier run is kept
    in the record, so a failure stays visible in history even after a fix. There is
    no command that sets a level to passed: only an executed run can.
    """
    if not contract:
        return {"status": "not_tested", "detail": "no acceptance contract", "stale": False}
    obligations = [o for o in contract.get("obligations", []) if o.get("level") == level]
    if not obligations:
        return {"status": "not_tested", "detail": f"contract has no {level} obligations"}
    passed, failed, missing, stale = [], [], [], False
    for ob in obligations:
        runs = [r for r in obligation_runs(record, ob["id"]) if r.get("executed")]
        if not runs:
            missing.append(ob["id"])
            continue
        last = runs[-1]
        if last.get("exit_code") == 0:
            passed.append(ob["id"])
            if code_changed_since is not None:
                moved = code_changed_since(last.get("head", ""))
                if moved is None or moved:
                    stale = True
        else:
            failed.append(ob["id"])
    total = len(obligations)
    if failed:
        status = "failed"
        detail = f"failed: {', '.join(failed)}"
    elif not missing:
        status = "passed"
        detail = f"{total}/{total} passed" + (
            " (code moved since; re-run before relying)" if stale else ""
        )
    elif blockers:
        status = "blocked"
        detail = "; ".join(b["reason"] for b in blockers)
    else:
        status = "not_tested"
        detail = f"{len(passed)}/{total} passed; not run: {', '.join(missing)}"
    return {"status": status, "detail": detail, "stale": stale}


def system_status(
    feat: dict,
    record: dict | None,
    contract: dict | None,
    *,
    close_blockers: list[str],
) -> dict:
    """Requirement verification state. Only ``passes: true`` is ever "verified".

    A close made before the evidence gate existed (``evidence: pre-gate``) is
    reported as verified with a ``legacy`` basis, never as proof: it was not
    re-verified, and at least one such close (ERR-3) has since been found false.
    """
    if feat.get("passes") is True:
        legacy = feat.get("evidence") == PRE_GATE
        return {
            "status": "verified",
            "basis": "legacy-pre-gate" if legacy else "evidence-gate",
            "detail": (
                "closed before the evidence gate existed; NOT re-verified"
                if legacy
                else "closed through the evidence gate"
            ),
        }
    runs_failed = []
    any_passed = False
    for oid, entry in ((record or {}).get("obligations") or {}).items():
        runs = [r for r in entry.get("runs") or [] if r.get("executed")]
        if runs and runs[-1].get("exit_code") != 0:
            runs_failed.append(oid)
        elif runs:
            any_passed = True
    steps = (record or {}).get("steps") or []
    failed_steps = [s.get("n") for s in steps if s.get("status") == "fail"]
    if any(s.get("status") == "pass" for s in steps):
        any_passed = True
    if runs_failed or failed_steps:
        what = [*runs_failed, *(f"step {n}" for n in failed_steps)]
        return {"status": "failed", "detail": f"failing evidence: {', '.join(map(str, what))}"}
    if close_blockers:
        return {
            "status": "blocked",
            "detail": f"needs prerequisite verification: {', '.join(close_blockers)}",
        }
    if feat.get("external_blocker"):
        return {"status": "blocked", "detail": f"external: {feat['external_blocker']}"}
    if any_passed:
        return {"status": "partially_verified", "detail": "some obligations evidenced"}
    return {"status": "not_verified", "detail": "no evidence recorded"}


def implementation_status(
    feat: dict,
    *,
    in_scope: bool,
    leased: bool,
    blockers: list[dict],
    parked: dict | None,
    code_landed: bool,
) -> dict:
    """One of IMPL_STATES, with the reason."""
    if implementation_complete(feat):
        return {"status": "complete", "detail": _impl_basis(feat)}
    if not in_scope:
        return {"status": "not_started", "detail": "outside the active release"}
    if feat.get("needs_clarification") is True:
        return {"status": "not_started", "detail": "needs clarification"}
    if leased:
        return {"status": "in_progress", "detail": "claimed by a session"}
    if blockers:
        return {"status": "blocked", "detail": "; ".join(b["reason"] for b in blockers)}
    if parked:
        return {
            "status": "blocked",
            "detail": f"parked ({parked.get('kind')}): {parked.get('reason')}",
        }
    if code_landed or implementation_partial(feat):
        return {
            "status": "ready",
            "detail": "code merged; finish implementation: acceptance contract, component "
            "evidence, independent review",
        }
    return {"status": "ready", "detail": "all implementation prerequisites met"}


def _impl_basis(feat: dict) -> str:
    if feat.get("passes") is True:
        return "requirement verified (implies implementation)"
    impl = feat.get("implementation") or {}
    return f"recorded {impl.get('ts', '?')} at {str(impl.get('commit', '?'))[:10]}"


# ----------------------------------------------------------------------------
# No executable work: the smallest set that restores progress
# ----------------------------------------------------------------------------
def _cover_key(item: tuple) -> tuple:
    """Implementation and unclassified blockers on one prerequisite are one action."""
    kind, dep, reason = item
    return ("interface", dep, reason) if kind == "interface" else ("implement", dep)


def blocker_report(
    blocked: dict[str, list[dict]],
    parked: dict,
    cycles: dict,
    close_blocked: dict[str, list[str]] | None = None,
) -> dict:
    """Greedy cover of the blocked set by the prerequisites that would free it.

    ``blocked`` maps a feature to its implementation blockers. Each blocker is
    either a feature to implement or a contract to define; greedily picking the
    one that touches the most blocked features gives a short, ordered list of what
    to do first. ``fully_freed`` counts only features left with no blocker at all.
    (A minimum hitting set is NP-hard; greedy is within ln(n).) ``close_blocked``
    (implemented features waiting on prerequisite VERIFICATION) is reduced to the
    unverified prerequisites ranked by how many closes each holds back, so a board
    with nothing left to build still names what to verify first.
    """
    remaining = {
        fid: {(b["type"], b["dep"], b["reason"]) for b in bs} for fid, bs in blocked.items()
    }
    cover: list[dict] = []
    while remaining:
        counts: dict[tuple, set] = {}
        for fid, items in remaining.items():
            for item in items:
                counts.setdefault(_cover_key(item), set()).add(fid)
        if not counts:
            break
        best, touched = max(counts.items(), key=lambda kv: (len(kv[1]), kv[0]))
        if best[0] == "interface":
            action = f"define the contract {best[1]} provides ({best[2]})"
        else:
            action = f"complete {best[1]}'s implementation"
        fully = []
        for fid in list(remaining):
            remaining[fid] = {i for i in remaining[fid] if _cover_key(i) != best}
            if not remaining[fid]:
                del remaining[fid]
                fully.append(fid)  # only a touched feature can empty in this step
        cover.append(
            {
                "action": action,
                "frees": sorted(touched),
                "fully_freed": sorted(fully),
                "prerequisite": best[1],
            }
        )
    verify_first: dict[str, set] = {}
    for fid, deps in (close_blocked or {}).items():
        for d in deps:
            verify_first.setdefault(d, set()).add(fid)
    decisions = sorted(
        (
            {"feature": f, "kind": p.get("kind"), "reason": p.get("reason")}
            for f, p in parked.items()
        ),
        key=lambda d: d["feature"],
    )
    return {
        "cover": cover,
        "verify_first": [
            {"prerequisite": d, "holds_back": sorted(f)}
            for d, f in sorted(verify_first.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ],
        "parked": decisions,
        "implementation_cycles": cycles.get("implementation", []),
    }
