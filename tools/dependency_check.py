#!/usr/bin/env python3
"""Contract gate for the typed dependency model and the acceptance contracts.

Fails when:

* ``tools/dependency_types.json`` is unreadable, classifies an edge that is not
  in ``tools/feature_deps.json``, uses an unknown type, omits a reason or a
  source, or gives an interface edge no contract (or an invalid pattern);
* the active-release graph contains an IMPLEMENTATION cycle - a loop of
  implementation-gating edges that no member can ever start;
* any ``docs/contracts/<id>.json`` is structurally invalid, or a non-draft
  contract cannot judge an implementation (no component test, a vague criterion,
  a system obligation that is not the acceptance criterion verbatim, ...).

Reports, without failing: deferred cycles (through integration or verification
edges - implementation proceeds; the requirement close needs a decision), open
edges still unclassified, and edges flagged for operator review.

    python3 tools/dependency_check.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import contracts  # noqa: E402
import dependency_model as dm  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def run(root: Path = ROOT) -> tuple[list[str], list[str]]:
    """(failures, notes) for the tree at ``root``."""
    import agent_pool  # noqa: PLC0415  (pure helper only: live_deps)

    failures: list[str] = []
    notes: list[str] = []
    features = json.loads((root / "feature_list.json").read_text(encoding="utf-8"))
    by_id = {f["id"]: f for f in features}
    deps_file = root / "tools" / "feature_deps.json"
    deps = json.loads(deps_file.read_text(encoding="utf-8")) if deps_file.exists() else {}
    try:
        types = dm.load_types(root / dm.TYPES_REL)
    except dm.DependencyModelError as exc:
        return [str(exc)], notes

    failures += [f"{dm.TYPES_REL}: {p}" for p in dm.validate_types(types, deps)]

    live = agent_pool.live_deps(deps, by_id)
    cycles = dm.cycle_report(live, types, by_id)
    for cycle in cycles["implementation"]:
        failures.append("implementation cycle (no member can ever start): " + " -> ".join(cycle))
    for cycle in cycles["deferred"]:
        notes.append(
            "deferred cycle (implementation proceeds; the close needs an operator decision): "
            + " -> ".join(cycle)
        )

    unclassified = sorted(
        dm.edge_key(a, b)
        for a, prereqs in live.items()
        for b in prereqs
        if a in by_id
        and b in by_id
        and by_id[a].get("passes") is not True
        and by_id[b].get("passes") is not True
        and dm.edge_type(types, a, b) == dm.UNCLASSIFIED
    )
    if unclassified:
        notes.append(
            f"{len(unclassified)} open edge(s) unclassified (they gate implementation): "
            + ", ".join(unclassified[:8])
        )
    review = sorted(k for k, m in (types.get("edges") or {}).items() if m.get("review"))
    if review:
        notes.append(f"{len(review)} classified edge(s) flagged for operator review")

    if root == contracts.ROOT:
        failures += [f"docs/contracts/{p}" for p in contracts.check_all()]
    return failures, notes


def main() -> int:
    failures, notes = run()
    for n in notes:
        print(f"  ⓘ {n}")
    for f in failures:
        print(f"✗ {f}")
    if failures:
        print(f"✗ dependency_check: {len(failures)} failure(s)")
        return 1
    print("✓ dependency_check: dependency types, implementation cycles, and contracts are sound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
