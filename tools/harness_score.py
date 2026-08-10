#!/usr/bin/env python3
"""Measure the harness, so its direction is a fact rather than an impression.

The audit that produced this work took a day of reading and returned a number. That
number is only useful if it can be recomputed cheaply — otherwise the next question
("is this getting better or worse?") costs another day, and so never gets asked.

Every input here is already produced by something else:

* ``.harness/runs/*/review.jsonl``  — rounds per feature, and which defect classes
  the reviewer actually raised (tools/adversarial_review.py)
* ``feature_list.json``             — evidence-backed vs pre-gate closes
* ``tools/gates.json``              — how much of the check corpus any gate runs
* ``cleanup_agents.sh --json``      — accumulated entropy

This is a TREND, not a grade. It deliberately does not compute the audit's 7.9/10:
a single number invites optimising the number. It reports the quantities the audit
actually moved, and appends them to .harness/metrics.json so two runs can be compared.

    tools/harness_score.py            # human
    tools/harness_score.py --json     # machine
    tools/harness_score.py --append   # …and record this point in the series
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import adversarial_review  # noqa: E402  (sibling module in tools/; path set just above)

ROOT = Path(__file__).resolve().parents[1]
METRICS = ROOT / ".harness" / "metrics.json"

# Rounds spent per feature BEFORE docs/playbooks/ existed. tools/playbook_harvest.py
# carries the same series; the claim "the playbooks are working" is falsifiable only
# against it.
PRE_PLAYBOOK_ROUNDS = (38, 20, 17, 15, 14, 14, 14, 13, 13, 13, 10, 9, 9)


def _median(xs) -> float | None:
    xs = list(xs)
    return round(statistics.median(xs), 1) if xs else None


def review_rounds() -> dict:
    """Rounds per feature, and the defect classes behind them."""
    runs = ROOT / ".harness" / "runs"
    per_feature: dict[str, int] = {}
    rule_features: dict[str, set] = {}
    if runs.is_dir():
        for jsonl in sorted(runs.glob("*/review.jsonl")):
            fid = jsonl.parent.name
            recs = adversarial_review.read_records(jsonl)
            if recs is None:
                continue  # unreadable is unknown, not zero
            # Attempts (a rate-limited Codex, a timed-out fallback) are recorded in
            # the same file but carry no verdict and no findings — counting them
            # would score reviewer downtime as review activity.
            n = 0
            for rec in recs:
                if not adversarial_review.is_round(rec):
                    continue
                n += 1
                for rule in rec.get("rules") or []:
                    rule_features.setdefault(rule, set()).add(fid)
            if n:
                per_feature[fid] = n
    recurring = sorted(
        ((r, len(f)) for r, f in rule_features.items() if len(f) >= 3),
        key=lambda kv: (-kv[1], kv[0]),
    )
    return {
        "features_measured": len(per_feature),
        "median_rounds": _median(per_feature.values()),
        "pre_playbook_median": _median(PRE_PLAYBOOK_ROUNDS),
        "promotion_candidates": [{"rule": r, "features": n} for r, n in recurring],
    }


def evidence_coverage() -> dict:
    """How much of `done` is actually backed by a record."""
    try:
        feats = json.loads((ROOT / "feature_list.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"error": "feature_list.json unreadable"}
    passing = [f for f in feats if f.get("passes") is True]
    runs = ROOT / ".harness" / "runs"
    evidenced = [
        f["id"]
        for f in passing
        if (runs / f["id"]).is_dir() and any((runs / f["id"]).glob("*.json"))
    ]
    return {
        "total": len(feats),
        "passing": len(passing),
        "pre_gate": sum(1 for f in passing if f.get("evidence") == "pre-gate"),
        "evidenced": len(evidenced),
        "unclassified_method": sum(
            1 for f in feats if not str(f.get("verification_method") or "").strip()
        ),
    }


def gate_coverage() -> dict:
    """How much of the check corpus each scope runs."""
    try:
        reg = json.loads((ROOT / "tools" / "gates.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"error": "gates.json unreadable"}
    on_disk = len(list((ROOT / "tools").glob("*_check.py")))
    scopes: dict[str, int] = {}
    for check in reg.get("checks", []):
        for scope in check.get("scopes", {}):
            scopes[scope] = scopes.get(scope, 0) + 1
    return {"registered": len(reg.get("checks", [])), "on_disk": on_disk, "by_scope": scopes}


def entropy() -> dict:
    """Whatever cleanup_agents.sh can see, without changing any of it."""
    script = ROOT / "tools" / "cleanup_agents.sh"
    if not script.is_file():
        return {"error": "cleanup_agents.sh missing"}
    proc = subprocess.run(
        [str(script), "--json"], cwd=str(ROOT), capture_output=True, text=True, check=False
    )
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"error": "cleanup_agents.sh --json produced no parsable output"}


def collect() -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "review": review_rounds(),
        "evidence": evidence_coverage(),
        "gates": gate_coverage(),
        "entropy": entropy(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--append", action="store_true", help="record this point in the series")
    args = ap.parse_args(argv)

    snap = collect()

    if args.append:
        METRICS.parent.mkdir(parents=True, exist_ok=True)
        series = []
        if METRICS.is_file():
            try:
                series = json.loads(METRICS.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                series = []
        series.append(snap)
        METRICS.write_text(json.dumps(series, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(snap, indent=2))
        return 0

    r, e, g, x = snap["review"], snap["evidence"], snap["gates"], snap["entropy"]
    print("== harness measurements ==")
    print(
        f"  review    : {r['features_measured']} feature(s) measured, median "
        f"{r['median_rounds']} round(s) vs pre-playbook {r['pre_playbook_median']}"
    )
    if r["promotion_candidates"]:
        top = r["promotion_candidates"][0]
        print(
            f"              {len(r['promotion_candidates'])} rule(s) seen on 3+ features "
            f"(top: {top['rule']} × {top['features']}) — candidates for critic_check.py"
        )
    if "error" not in e:
        print(
            f"  evidence  : {e['passing']}/{e['total']} passing — {e['evidenced']} evidenced, "
            f"{e['pre_gate']} pre-gate (never re-verified)"
        )
        print(f"              {e['unclassified_method']} feature(s) with no verification_method")
    if "error" not in g:
        print(
            f"  gates     : {g['registered']}/{g['on_disk']} checks registered; "
            f"by scope {g['by_scope']}"
        )
    if "error" not in x:
        print(
            f"  entropy   : {x.get('worktrees_removable', '?')} worktree(s) removable, "
            f"{x.get('stale_branches', '?')} stale branch(es), "
            f"{x.get('orphan_plans', '?')} orphan plan(s), "
            f"{x.get('leaked_scratch', '?')} leaked scratch dir(s)"
        )
    print(
        "\n  A trend, not a grade. Compare two runs; a single number invites optimising the number."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
