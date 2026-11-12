#!/usr/bin/env python3
"""Feature acceptance contracts and requirement traceability.

A contract (``docs/contracts/<FEATURE_ID>.json``) is what a feature's
implementation is judged against. It splits the feature's acceptance criterion
by the level at which each part can honestly be proven:

* ``component``   - objective criteria proven with test doubles, each with at
                    least one re-executable test command. These decide
                    *implementation complete*.
* ``integration`` - criteria that need real peers (named in ``needs``).
* ``system``      - exactly one obligation whose criterion is the feature's
                    acceptance criterion, VERBATIM. This is what ``passes: true``
                    asserts, so splitting work into levels can never weaken it.

Contracts live beside the docs, not in ``feature_list.json``, because a session
authors its contract on its branch and ``feature_list.json`` is integrator-owned.
The acceptance criterion itself stays in ``feature_list.json`` and ``docs/SRS.md``;
the contract only refers to it, and validation fails if they drift apart.

    python3 tools/contracts.py check                 # validate every contract
    python3 tools/contracts.py draft SRS-MD-004      # write a skeleton to fill in
    python3 tools/contracts.py show SRS-MD-004       # contract + trace
    python3 tools/contracts.py requirements [--json] # SyRS -> features -> status
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dependency_model as dm  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = ROOT / "docs" / "contracts"
SRS_FILE = ROOT / "docs" / "SRS.md"
FEATURE_FILE = ROOT / "feature_list.json"

LEVELS = ("component", "integration", "system")
KNOWN_RELEASES = ("MVP", "R2")  # mirrors agent_pool.KNOWN_RELEASES
LEVEL_PREFIX = {"component": "C", "integration": "I", "system": "S"}
# Criteria that cannot be checked by anyone. A contract exists to replace these.
VAGUE = re.compile(
    r"\b(fully functional|works? (correctly|properly|as expected)|as expected|"
    r"properly|appropriate(ly)?|robust(ly)?|user[- ]friendly|TODO|TBD)\b",
    re.IGNORECASE,
)
SYRS_ID = re.compile(r"\b(?:SYS-\d+[a-z]?|NFR-[A-Z]+\d+[a-z]?|AC-\d+|IF-\d+)\b")
STRS_ID = re.compile(r"\b(?:SN-\d+\.\d+|BG-\d+|C-\d+|SC-\d+)\b")
ROW_ID = re.compile(r"^\|\s*((?:SRS-[A-Z]+-\d+)|(?:UI|API|ERR)-\d+)\s*\|")
SRS_REF = re.compile(r"\b(?:SRS-[A-Z]+-\d+|(?:UI|API|ERR)-\d+)\b")


class ContractError(Exception):
    """A contract file is unreadable or not an object."""


# ----------------------------------------------------------------------------
# Acceptance criterion
# ----------------------------------------------------------------------------
def ac_text(feat: dict) -> str:
    """The acceptance-criterion step, without its ``Step N:`` prefix."""
    steps = [str(s) for s in feat.get("steps", [])]
    idx = next(
        (
            i
            for i, s in enumerate(steps)
            if "acceptance criteria" in s.lower() or "expected behavior" in s.lower()
        ),
        2 if len(steps) > 2 else None,
    )
    if idx is None:
        return ""
    return re.sub(r"^Step \d+:\s*", "", steps[idx]).strip()


# ----------------------------------------------------------------------------
# Traceability: SRS rows -> SyRS / StRS
# ----------------------------------------------------------------------------
def parse_srs(text: str) -> dict[str, dict]:
    """Every requirement row's trace cell, resolved to SyRS and StRS ids.

    Section 5 rows trace straight to ``SyRS: …; StRS: …``. Section 6-8 rows (UI-*,
    API-*, ERR-*) trace to SRS ids instead, so they inherit those rows' links. The
    LAST matching row wins, which skips the §3.1 Split table that reuses the ids.
    """
    raw: dict[str, str] = {}
    for line in text.splitlines():
        m = ROW_ID.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        raw[m.group(1)] = cells[-1]
    out: dict[str, dict] = {}

    def resolve(rid: str, seen: set) -> dict:
        if rid in out:
            return out[rid]
        trace = raw.get(rid, "")
        syrs = set(SYRS_ID.findall(trace))
        strs = set(STRS_ID.findall(trace))
        via = sorted(r for r in SRS_REF.findall(trace) if r != rid)
        if not syrs and via:
            for ref in via:
                if ref in seen or ref not in raw:
                    continue
                sub = resolve(ref, seen | {rid})
                syrs |= set(sub["syrs"])
                strs |= set(sub["strs"])
        result = {
            "syrs": sorted(syrs),
            "strs": sorted(strs),
            "via": via if not SYRS_ID.search(trace) else [],
        }
        out[rid] = result
        return result

    for rid in raw:
        resolve(rid, set())
    return out


def load_trace(srs_file: Path | None = None) -> dict[str, dict]:
    srs_file = SRS_FILE if srs_file is None else srs_file
    if not srs_file.exists():
        raise ContractError(f"{srs_file} not found")
    return parse_srs(srs_file.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------
# Contracts
# ----------------------------------------------------------------------------
def contract_path(fid: str, contracts_dir: Path | None = None) -> Path:
    contracts_dir = CONTRACTS_DIR if contracts_dir is None else contracts_dir
    return contracts_dir / f"{fid}.json"


def load_contract(fid: str, contracts_dir: Path | None = None) -> dict | None:
    """The feature's contract, or None when it has none. Corrupt raises."""
    contracts_dir = CONTRACTS_DIR if contracts_dir is None else contracts_dir
    path = contract_path(fid, contracts_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError(f"{path.name} is corrupt: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError(f"{path.name}: a contract must be a JSON object")
    return data


def contract_text(fid: str, contracts_dir: Path | None = None) -> str:
    contracts_dir = CONTRACTS_DIR if contracts_dir is None else contracts_dir
    path = contract_path(fid, contracts_dir)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def gating_prereqs(fid: str, deps: dict, types: dict) -> list[str]:
    return sorted(
        d for d in deps.get(fid, []) if dm.edge_type(types, fid, d) in dm.IMPLEMENTATION_GATING
    )


def validate(
    contract: dict,
    feat: dict,
    *,
    deps: dict,
    types: dict,
    trace: dict[str, dict],
    reexecutable=None,
) -> list[str]:
    """Every reason this contract cannot yet judge an implementation.

    A ``draft`` contract still has to be structurally sound, but it is reported
    as a problem, because a draft cannot decide that implementation is complete.
    """
    fid = feat.get("id", "?")
    problems: list[str] = []
    if contract.get("feature") != fid:
        problems.append(f"'feature' is {contract.get('feature')!r}, expected {fid!r}")
    if contract.get("draft") is True:
        problems.append("contract is a draft: author the component criteria, then drop 'draft'")

    reqs = contract.get("requirements")
    known = set((trace.get(fid) or {}).get("syrs", []))
    if not isinstance(reqs, list) or not reqs:
        problems.append("'requirements' must list the SyRS ids this feature traces to")
    elif known:
        unknown = sorted(set(reqs) - known)
        if unknown:
            problems.append(
                f"requirements {unknown} are not in this feature's docs/SRS.md trace {sorted(known)}"
            )
    if not str(contract.get("scope") or "").strip():
        problems.append("'scope' must say what this feature implements")
    paths = contract.get("implementation_paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
        problems.append(
            "'implementation_paths' must list the repo files that implement this feature "
            "(the reviewer is shown them; for code already on main the diff alone shows nothing)"
        )
    elif any(p.startswith("/") or ".." in Path(p).parts for p in paths):
        problems.append("'implementation_paths' must be repo-relative, without '..'")

    ifaces = contract.get("interfaces")
    if not isinstance(ifaces, dict) or not {"consumes", "provides"} <= set(ifaces):
        problems.append(
            "'interfaces' must have 'consumes' and 'provides' lists (either may be empty)"
        )

    expected_prereqs = gating_prereqs(fid, deps, types)
    if sorted(contract.get("prerequisites") or []) != expected_prereqs:
        problems.append(
            f"'prerequisites' {sorted(contract.get('prerequisites') or [])} must equal this "
            f"feature's implementation-gating dependencies {expected_prereqs} "
            "(classify the edge in tools/dependency_types.json to change that set)"
        )

    obligations = contract.get("obligations")
    if not isinstance(obligations, list) or not obligations:
        problems.append("'obligations' must be a non-empty list")
        return problems
    ids: set[str] = set()
    by_level: dict[str, list[dict]] = {lvl: [] for lvl in LEVELS}
    for ob in obligations:
        oid = str(ob.get("id") or "")
        level = ob.get("level")
        if level not in LEVELS:
            problems.append(f"{oid or '?'}: level {level!r} is not one of {LEVELS}")
            continue
        if not re.fullmatch(LEVEL_PREFIX[level] + r"\d+", oid):
            problems.append(f"{oid or '?'}: a {level} obligation id is {LEVEL_PREFIX[level]}<n>")
        if oid in ids:
            problems.append(f"{oid}: duplicate obligation id")
        ids.add(oid)
        by_level[level].append(ob)
        criterion = str(ob.get("criterion") or "").strip()
        if not criterion:
            problems.append(f"{oid}: empty criterion")
        elif level != "system" and VAGUE.search(criterion):
            problems.append(
                f"{oid}: criterion is not objective ({VAGUE.search(criterion).group(0)!r}); "
                "state an input, an observable output, and the test that checks it"
            )
        if level == "component":
            tests = ob.get("tests") or []
            if not tests:
                problems.append(f"{oid}: a component obligation needs at least one test command")
            for cmd in tests:
                if reexecutable is not None and not reexecutable(str(cmd)):
                    problems.append(
                        f"{oid}: test {cmd!r} is not re-executable by the integrator "
                        "(tools/evidence.py REEXECUTABLE), so its result could only be testimony"
                    )
        if level == "integration":
            for need in ob.get("needs") or []:
                if need not in deps.get(fid, []):
                    problems.append(
                        f"{oid}: needs {need}, which is not a dependency of {fid} in "
                        "tools/feature_deps.json"
                    )
    if not by_level["component"]:
        problems.append(
            "no component obligation: if nothing here can be component-tested, say so in "
            "'limitations' and keep the feature out of implementation-complete"
        )
    system = by_level["system"]
    if len(system) != 1:
        problems.append(f"exactly one system obligation is required, found {len(system)}")
    elif str(system[0].get("criterion") or "").strip() != ac_text(feat):
        problems.append(
            "the system obligation's criterion must equal the feature's acceptance criterion "
            "verbatim (feature_list.json): a contract may add levels, never weaken the requirement"
        )
    return problems


def draft(fid: str, feat: dict, *, deps: dict, types: dict, trace: dict[str, dict]) -> dict:
    """A skeleton with everything derivable filled in and the judgment left blank."""
    t = trace.get(fid) or {}
    needs_integration = sorted(
        d for d in deps.get(fid, []) if dm.edge_type(types, fid, d) == "integration"
    )
    obligations = [
        {"id": "C1", "level": "component", "criterion": "", "tests": []},
    ]
    if needs_integration:
        obligations.append(
            {
                "id": "I1",
                "level": "integration",
                "criterion": "",
                "needs": needs_integration,
                "tests": [],
            }
        )
    obligations.append(
        {
            "id": "S1",
            "level": "system",
            "criterion": ac_text(feat),
            "method": feat.get("verification_method", ""),
        }
    )
    return {
        "feature": fid,
        "draft": True,
        "requirements": t.get("syrs", []),
        "stakeholder_needs": t.get("strs", []),
        "scope": feat.get("description", ""),
        "implementation_paths": [],
        "interfaces": {"consumes": [], "provides": []},
        "prerequisites": gating_prereqs(fid, deps, types),
        "obligations": obligations,
        "completion_evidence": [
            "tools/evidence.py run <ID> --obligation C<n> -- <test> for every component obligation",
            "an approving tools/adversarial_review.py round at the evidenced commit",
        ],
        "limitations": [],
    }


# ----------------------------------------------------------------------------
# Requirement-level status (multiple features -> one SyRS requirement)
# ----------------------------------------------------------------------------
def requirement_status(
    trace: dict[str, dict], features: list[dict], *, active_release: str
) -> dict:
    """SyRS id -> its contributing features and an aggregate verification state.

    A system requirement is ``verified`` only when EVERY in-release feature that
    traces to it is requirement-verified. One feature passing never verifies a
    requirement other features also carry. ``verified (legacy)`` marks a set that
    includes pre-gate closes nobody re-verified.
    """
    by_id = {f["id"]: f for f in features}
    reqs: dict[str, list[str]] = {}
    for fid, t in trace.items():
        if fid not in by_id:
            continue
        for sid in t.get("syrs", []):
            reqs.setdefault(sid, []).append(fid)
    out: dict[str, dict] = {}
    for sid, fids in sorted(reqs.items()):
        # Deferred means EXPLICITLY tagged for another known release. An untagged or
        # misspelled release stays in scope (unknown fails closed), as in agent_pool.
        in_scope = [
            f
            for f in sorted(fids)
            if not (
                by_id[f].get("release") in KNOWN_RELEASES
                and by_id[f].get("release") != active_release
            )
        ]
        deferred = [f for f in sorted(fids) if f not in in_scope]
        passing = [f for f in in_scope if by_id[f].get("passes") is True]
        legacy = [f for f in passing if by_id[f].get("evidence") == dm.PRE_GATE]
        if not in_scope:
            state = "deferred"
        elif len(passing) == len(in_scope):
            state = "verified (legacy)" if legacy else "verified"
        elif passing:
            state = "partially_verified"
        else:
            state = "not_verified"
        out[sid] = {
            "status": state,
            "features": in_scope,
            "verified": passing,
            "open": [f for f in in_scope if f not in passing],
            "legacy_pre_gate": legacy,
            "deferred_features": deferred,
        }
    return out


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _load_all():
    import agent_pool  # noqa: PLC0415  (heavy import; CLI only)

    # Read the graph of THIS tree (script-relative), not agent_pool.ROOT, which is
    # always the primary checkout: a check run in a worktree must judge the worktree.
    features = json.loads(FEATURE_FILE.read_text(encoding="utf-8"))
    deps_file = ROOT / "tools" / "feature_deps.json"
    raw = json.loads(deps_file.read_text(encoding="utf-8")) if deps_file.exists() else {}
    deps = agent_pool.live_deps(raw, {f["id"]: f for f in features})
    types = dm.load_types(ROOT / dm.TYPES_REL)
    return agent_pool, features, deps, types


def check_all(contracts_dir: Path | None = None) -> list[str]:
    """Problems across every contract file. Drafts are reported but not fatal."""
    contracts_dir = CONTRACTS_DIR if contracts_dir is None else contracts_dir
    import evidence  # noqa: PLC0415

    _ap, features, deps, types = _load_all()
    by_id = {f["id"]: f for f in features}
    trace = load_trace()
    problems: list[str] = []
    if not contracts_dir.is_dir():
        return problems
    for path in sorted(contracts_dir.glob("*.json")):
        fid = path.stem
        if fid not in by_id:
            problems.append(f"{path.name}: no feature {fid} in feature_list.json")
            continue
        try:
            contract = load_contract(fid, contracts_dir)
        except ContractError as exc:
            problems.append(str(exc))
            continue
        issues = validate(
            contract,
            by_id[fid],
            deps=deps,
            types=types,
            trace=trace,
            reexecutable=evidence.reexecutable,
        )
        fatal = [i for i in issues if not i.startswith("contract is a draft")]
        if contract.get("draft") is True:
            # A draft may leave the judgment blank; it may not be structurally wrong.
            fatal = [
                i
                for i in fatal
                if "empty criterion" not in i and "needs at least one test" not in i
            ]
        problems += [f"{fid}: {i}" for i in fatal]
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="validate every contract under docs/contracts/")
    d = sub.add_parser("draft", help="write a skeleton contract for a feature")
    d.add_argument("id")
    d.add_argument("--force", action="store_true", help="overwrite an existing contract")
    s = sub.add_parser("show", help="print a feature's contract and trace")
    s.add_argument("id")
    r = sub.add_parser("requirements", help="SyRS requirement -> features -> status")
    r.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "check":
        problems = check_all()
        for p in problems:
            print(f"✗ {p}")
        n = len(list(CONTRACTS_DIR.glob("*.json"))) if CONTRACTS_DIR.is_dir() else 0
        print(f"{'✓' if not problems else '✗'} {n} contract(s); {len(problems)} problem(s)")
        return 1 if problems else 0

    agent_pool, features, deps, types = _load_all()
    by_id = {f["id"]: f for f in features}
    trace = load_trace()

    if args.cmd == "draft":
        if args.id not in by_id:
            print(f"✗ unknown feature {args.id}", file=sys.stderr)
            return 1
        path = contract_path(args.id)
        if path.exists() and not args.force:
            print(f"✗ {path.relative_to(ROOT)} exists (use --force to overwrite)", file=sys.stderr)
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        body = draft(args.id, by_id[args.id], deps=deps, types=types, trace=trace)
        path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        print(f"✓ wrote {path.relative_to(ROOT)} (draft: fill in the component criteria and tests)")
        return 0

    if args.cmd == "show":
        print(
            json.dumps({"trace": trace.get(args.id), "contract": load_contract(args.id)}, indent=2)
        )
        return 0

    status = requirement_status(trace, features, active_release=agent_pool.ACTIVE_RELEASE)
    if args.json:
        print(json.dumps(status, indent=2))
        return 0
    counts: dict[str, int] = {}
    for v in status.values():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    print("== SyRS requirements by verification state ==")
    print("   " + "  ".join(f"{k}:{n}" for k, n in sorted(counts.items())))
    for sid, v in status.items():
        if v["status"] in ("verified", "deferred"):
            continue
        print(f"  {sid:10} {v['status']:20} open: {', '.join(v['open']) or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
