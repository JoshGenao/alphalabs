#!/usr/bin/env python3
"""The verification queue — what is stuck, why, and what would actually move it.

``agent_pool.py status`` has one bucket labelled *awaiting human verification* and
it was, for a long time, eleven features in four unrelated situations. Reading
which was which meant reading eleven session notes totalling ~2,000 lines, every
fresh-context session, and the notes disagree with the graph often enough that
sessions re-derived it and got it wrong.

This answers it from data instead:

    tools/verify_queue.py list              # the ranked worklist
    tools/verify_queue.py list --markdown   # the same, as an issue body
    tools/verify_queue.py show <FID>        # everything needed to verify one
    tools/verify_queue.py check             # DRIFT only — exit 1 if anything moved
    tools/verify_queue.py run <FID>         # the exclusive-box lane

The four classes (docs/verification-queue.md):

    A  actionable   nothing but evidence stands between it and a close
    B  blocked      an unbuilt FEATURE is in the way (a real dep edge)
    C  external     a real-world resource no feature owns (external_blocker)
    D  cycle        the dependency graph contradicts itself

Class is DERIVED, never declared: C if it carries an ``external_blocker``, D if it
sits on a cycle, B if it has unmet deps, A otherwise. A feature cannot be filed
wrongly by omission, because every feature is one of the four.

``check`` is the loop's entry point. It reports only what CHANGED — a feature whose
last blocker just closed, an untriaged one, a record invalidated by a re-spec — and
exits non-zero when there is something to act on, so a scheduler can run it
unattended without narrating a steady state nobody reads.

Exit codes: 0 = nothing to act on, 1 = drift found (``check``) or the step failed
(``run``), 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_pool  # noqa: E402  (sibling module in tools/; path set just above)
import evidence  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
NOTES_DIR = ROOT / "progress.d"

CLASS_LABEL = {
    "A": "actionable",
    "B": "blocked on a feature",
    "C": "blocked on an external resource",
    "D": "dependency cycle",
}

# Which pytest marker implies which verification_method. Ordered most-binding
# first: a file carrying both `integration` and `live_broker` is a live-IB file,
# because the single-live invariant is what constrains scheduling.
MARKER_METHOD = (
    ("live_broker", "live-ib"),
    ("e2e", "e2e"),
    ("integration", "integration"),
)
SOLO_MARKERS = ("unit", "property", "boundary", "contract", "domain", "safety")

MARK_RE = re.compile(r"pytest\.mark\.([a-z_]+)")


# ---------------------------------------------------------------------------
# Test discovery — what artifacts actually exist for a feature
# ---------------------------------------------------------------------------
def _fid_forms(fid: str) -> tuple:
    """The spellings a feature id takes in source: SRS-MD-003 / srs_md_003 / md003.

    The compact form is what test FILENAMES use (`test_data013_...`), but deriving
    it by dropping the first segment produces `"3"` for `API-3` and `"1"` for
    `UI-1` — and a bare digit is a substring of very nearly every source file, so
    every one of those features matched the whole tree. `run` EXECUTES this
    selection, so a false match is not cosmetic: it files an unrelated passing
    suite as a feature's evidence. Require the compact form to carry a letter and
    real length, and drop it otherwise — the hyphenated and snake spellings are
    specific enough on their own.
    """
    lower = fid.lower()
    snake = lower.replace("-", "_")
    compact = re.sub(r"[^a-z0-9]", "", lower.split("-", 1)[-1])  # md003, data013
    if len(compact) < 4 or not any(c.isalpha() for c in compact):
        compact = ""
    return (fid, snake, compact)


def discover_tests(fid: str) -> dict:
    """Test files that plausibly belong to this feature, scored by how they name it.

    A bare grep is too noisy to act on: `tests/unit/test_adversarial_review.py`
    mentions SRS-MD-003 as an example string, and treating that as MD-003's test
    would put a harness test in a feature's evidence. So matches are ranked and the
    reason is reported, because the caller (`run`) is about to EXECUTE these and a
    human has to be able to check the selection.

      strong — the id is in the filename, or in the module docstring / first 40
               lines, or in a test function's own name
      weak   — the id appears somewhere else in the file

    Returns {"strong": [...], "weak": [...], "markers": {path: [marker, ...]}}.
    """
    fid_re, snake, compact = _fid_forms(fid)
    strong, weak, markers = [], [], {}
    if not TESTS_DIR.is_dir():
        return {"strong": [], "weak": [], "markers": {}}
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        low = text.lower()
        if not any(form.lower() in low for form in (fid_re, snake, compact) if form):
            continue
        rel = str(path.relative_to(ROOT))
        head = "\n".join(text.splitlines()[:40]).lower()
        named_in_a_test = any(
            any(form.lower() in line.lower() for form in (fid_re, snake, compact) if form)
            for line in text.splitlines()
            if line.lstrip().startswith("def test_")
        )
        if compact and compact in path.name.lower():
            strong.append(rel)
        elif fid_re.lower() in head or snake in head or named_in_a_test:
            strong.append(rel)
        else:
            weak.append(rel)
        found = sorted(set(MARK_RE.findall(text)))
        if found:
            markers[rel] = found
    return {"strong": strong, "weak": weak, "markers": markers}


def method_from_markers(discovery: dict, *, strong_only: bool = True) -> tuple:
    """Propose a verification_method from the markers on the feature's OWN tests.

    This is what CLAUDE.md rule 5 asks for — verify the artifact, not the intention.
    The templated ``steps[]`` prose says what a generated template said; a
    `@pytest.mark.e2e` on a file named for the feature says what running it needs.

    WHAT THIS CANNOT SEE, and it is the whole reason the answer is a PROPOSAL:
    a feature's tests and its acceptance criterion can disagree, legitimately.
    SRS-MD-003's discovered tests are entirely fixture-based, so this returns
    ``solo`` — and it is right about the tests and wrong about the feature, whose
    AC ("IB Gateway heartbeat staleness ... is detected") is only satisfiable
    against a real gateway. Tests prove the mechanism; the AC quantifies over the
    deployed path. Never apply this without reading steps[2] alongside it.

    Returns (method, why). ``None`` when nothing was discovered — absent is not
    solo (CLAUDE.md rule 3), so the caller must not read a miss as permission.
    """
    files = discovery["strong"] if strong_only else discovery["strong"] + discovery["weak"]
    seen = {m for f in files for m in discovery["markers"].get(f, [])}
    if not seen:
        return (None, "no tests discovered for this feature")
    for marker, method in MARKER_METHOD:
        if marker in seen:
            return (method, f"a discovered test is marked @pytest.mark.{marker}")
    if seen & set(SOLO_MARKERS):
        kinds = ", ".join(sorted(seen & set(SOLO_MARKERS)))
        return ("solo", f"discovered tests carry only in-process markers ({kinds})")
    return (None, f"markers present but none are decisive ({', '.join(sorted(seen))})")


# ---------------------------------------------------------------------------
# Board state
# ---------------------------------------------------------------------------
def graph_cycles(deps: dict) -> list:
    """Every cycle in the dependency graph, as paths.

    `block` refuses to create one, but SEED_DEPS and hand edits do not go through
    `block`, and a cycle is invisible in `status` — it presents as a set of
    features that are permanently blocked for no stated reason. Four features sat
    that way for six weeks.
    """
    cycles, seen = [], set()
    for fid in sorted(deps):
        for dep in deps.get(fid, []):
            path = agent_pool.dep_path(deps, dep, fid)
            if path is None:
                continue
            key = frozenset([fid, *path])
            if key in seen:
                continue
            seen.add(key)
            cycles.append([fid, *path])
    return cycles


def classify(fid: str, feat: dict, blocked: dict, cycle_members: set) -> str:
    if agent_pool.external_blocker(feat):
        return "C"
    if fid in cycle_members:
        return "D"
    if blocked.get(fid):
        return "B"
    return "A"


def evidence_state(fid: str, features: list) -> dict:
    """What the record says, without deciding what it means.

    ``allow_attested=True`` on purpose: this is a report, and a hand-recorded live
    step IS the evidence for a live-IB feature. Reporting it as missing would tell
    the operator to redo work they already did. The GATE
    (close_feature.py) applies the strict reading; this does not gate anything.
    """
    try:
        ok, problems, summary = evidence.verify(fid, features, allow_attested=True)
    except evidence.EvidenceError as exc:
        return {"ok": False, "problems": [f"record unreadable: {exc}"], "summary": {}}
    return {"ok": ok, "problems": problems, "summary": summary}


def resume_block(fid: str) -> str:
    """The session note's own `Resume / next` paragraph, which names the owners."""
    path = NOTES_DIR / f"session-{fid}.md"
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out, capturing = [], False
    for line in lines:
        if re.match(r"^\s*resume\s*/\s*next", line, re.I):
            capturing, out = True, [line.strip()]
            continue
        if capturing:
            if line.strip().startswith("=== SESSION") or line.strip() == "---":
                break
            out.append(line.rstrip())
            if len(out) > 14:
                break
    return "\n".join(out).strip()


def build_queue(features=None, deps=None, runtime=None) -> dict:
    features = features if features is not None else agent_pool.load_features(fetch=False)
    deps = deps if deps is not None else agent_pool.load_deps()
    runtime = runtime if runtime is not None else agent_pool.load_runtime()

    by_id = {f["id"]: f for f in features}
    _ready, blocked, active, _held, _ = agent_pool.compute(features, deps, runtime)
    impact = agent_pool.impact_scores(deps, by_id)
    cycles = graph_cycles(deps)
    cycle_members = {fid for path in cycles for fid in path}
    serialized = agent_pool.serialized_notes()

    rows = []
    for fid, feat in by_id.items():
        if feat.get("passes") is True or feat.get("needs_clarification") is True:
            continue
        cls = classify(fid, feat, blocked, cycle_members)
        rows.append(
            {
                "id": fid,
                "class": cls,
                "impact": impact.get(fid, 0),
                "priority": feat.get("priority", "?"),
                "method": str(feat.get("verification_method") or "").strip() or "(unclassified)",
                "external_blocker": agent_pool.external_blocker(feat),
                "unmet": blocked.get(fid, []),
                "serialized_note": fid in serialized,
                "leased": fid in active,
                "ac": (feat.get("steps") or ["", "", ""])[2]
                if len(feat.get("steps") or []) > 2
                else "",
            }
        )
    rows.sort(key=lambda r: (r["class"], -r["impact"], r["id"]))
    return {
        "rows": rows,
        "cycles": cycles,
        "by_id": by_id,
        "features": features,
        "deps": deps,
        "impact": impact,
    }


# ---------------------------------------------------------------------------
# Drift — the only thing a loop should report
# ---------------------------------------------------------------------------
def find_drift(queue: dict) -> list:
    """What CHANGED, or what is inconsistent. Steady state produces nothing.

    A watcher that prints the whole board every run is a watcher whose output stops
    being read, and then the one run that mattered scrolls past with the rest.
    """
    findings = []
    deps = queue["deps"]

    for path in queue["cycles"]:
        findings.append(
            {
                "kind": "cycle",
                "severity": "high",
                "id": path[0],
                "detail": "dependency cycle: " + " -> ".join(path),
                "action": f"agent_pool.py unblock {path[-2]} --off {path[-1]} "
                f"--reason '<which direction is a code edge>'",
            }
        )

    for row in queue["rows"]:
        fid = row["id"]
        # A serialized note is the ONLY thing holding some features off the
        # frontier, and a note is not a graph edge. If it names no blocker the
        # scheduler can see, nobody can tell whether it is done or abandoned.
        if row["serialized_note"] and row["class"] == "A":
            st = evidence_state(fid, queue["features"])
            findings.append(
                {
                    "kind": "actionable",
                    "severity": "high" if st["ok"] else "medium",
                    "id": fid,
                    "detail": (
                        f"nothing blocks it but evidence — unblocks {row['impact']}; "
                        + (
                            "record is COMPLETE, it can close now"
                            if st["ok"]
                            else f"{len(st['problems'])} evidence gap(s)"
                        )
                    ),
                    "action": (
                        f"close_feature.py {fid} --verified --attested-by operator"
                        if st["ok"]
                        else f"verify_queue.py show {fid}"
                    ),
                }
            )
        if row["serialized_note"] and row["class"] == "A" and not deps.get(fid):
            findings.append(
                {
                    "kind": "untriaged",
                    "severity": "medium",
                    "id": fid,
                    "detail": "held off the frontier by a session note alone — no dep "
                    "edge, no external_blocker. Nothing records WHY.",
                    "action": f"agent_pool.py block {fid} --on <owner ids>, or add an "
                    f'"external_blocker" to feature_list.json',
                }
            )
        # A record bound to a spec that has since changed is not evidence of the
        # current criteria. evidence.verify already says so; nobody was reading it
        # until a close was attempted, which is the worst moment to find out.
        if evidence.record_path(fid).exists():
            st = evidence_state(fid, queue["features"])
            stale = [p for p in st["problems"] if "different specification" in p]
            if stale:
                findings.append(
                    {
                        "kind": "stale-evidence",
                        "severity": "high",
                        "id": fid,
                        "detail": stale[0],
                        "action": f"re-run the affected steps: evidence.py run {fid} --step N -- ...",
                    }
                )

    # A feature marked passing that was never evidenced. Honest bookkeeping, not an
    # accusation — but it must not read as verified when `status` counts it as done.
    pre_gate = [
        f["id"]
        for f in queue["features"]
        if f.get("passes") is True and f.get("evidence") == evidence.PRE_GATE
    ]
    if pre_gate:
        findings.append(
            {
                "kind": "pre-gate",
                "severity": "low",
                "id": f"{len(pre_gate)} features",
                "detail": f"{len(pre_gate)} passing feature(s) closed before the evidence "
                f"gate existed and were never re-verified",
                "action": "no action required; do not read `done` as `evidenced`",
            }
        )
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3), f["id"]))
    return findings


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _render_text(queue: dict) -> str:
    out = []
    counts = {}
    for row in queue["rows"]:
        counts[row["class"]] = counts.get(row["class"], 0) + 1
    out.append(
        "== verification queue == "
        + "  ".join(f"{k}:{counts.get(k, 0)} {CLASS_LABEL[k]}" for k in "ABCD")
    )
    for cls in "ABCD":
        rows = [r for r in queue["rows"] if r["class"] == cls]
        if not rows:
            continue
        out.append(f"\n-- {cls}: {CLASS_LABEL[cls]} --")
        for r in rows:
            why = (
                r["external_blocker"]
                or (("blocked-on " + ", ".join(r["unmet"])) if r["unmet"] else "")
                or ("serialized note; no recorded blocker" if r["serialized_note"] else "ready")
            )
            out.append(
                f"  {r['id']:18} {r['priority']:3} unblocks:{r['impact']:<4} "
                f"{r['method']:<12} {why[:70]}"
            )
    return "\n".join(out)


def _render_markdown(queue: dict, drift: list) -> str:
    out = ["## Verification queue", ""]
    if drift:
        out += ["### Needs a decision", ""]
        for f in drift:
            out.append(f"- **{f['severity'].upper()}** · `{f['id']}` — {f['detail']}")
            out.append(f"  - `{f['action']}`")
        out.append("")
    else:
        out += ["Nothing has drifted since the last run.", ""]
    out += [
        "### The board",
        "",
        "| class | feature | unblocks | method | what is in the way |",
        "|---|---|---:|---|---|",
    ]
    for r in queue["rows"]:
        why = (
            r["external_blocker"]
            or (("blocked-on " + ", ".join(f"`{d}`" for d in r["unmet"])) if r["unmet"] else "")
            or (
                "serialized note; no recorded blocker" if r["serialized_note"] else "ready to claim"
            )
        )
        out.append(f"| {r['class']} | `{r['id']}` | {r['impact']} | {r['method']} | {why} |")
    out += [
        "",
        "Classes: **A** actionable · **B** blocked on a feature · "
        "**C** blocked on an external resource · **D** dependency cycle. "
        "See `docs/verification-queue.md`.",
    ]
    return "\n".join(out)


def _render_show(fid: str, queue: dict) -> str:
    feat = queue["by_id"].get(fid)
    if feat is None:
        return f"✗ unknown feature id: {fid}"
    row = next((r for r in queue["rows"] if r["id"] == fid), None)
    st = evidence_state(fid, queue["features"])
    disc = discover_tests(fid)
    proposed, why = method_from_markers(disc)

    out = [f"== {fid} — {feat.get('description', '')}", ""]
    if row:
        out.append(f"class     {row['class']} ({CLASS_LABEL[row['class']]})")
        out.append(f"impact    unblocks {row['impact']} feature(s)")
    out.append(f"method    {str(feat.get('verification_method') or '(unclassified)')}")
    if agent_pool.external_blocker(feat):
        out.append(f"external  {agent_pool.external_blocker(feat)}")
    if row and row["unmet"]:
        out.append(f"blocked   on {', '.join(row['unmet'])}")

    out += ["", "-- acceptance criteria (steps[3]) --"]
    steps = feat.get("steps") or []
    out.append("  " + (steps[2] if len(steps) > 2 else "(none recorded)"))

    out += ["", "-- evidence --"]
    summ = st["summary"]
    out.append(
        f"  {summ.get('steps_evidenced', 0)}/{summ.get('steps_total', len(steps))} steps; "
        f"critics: {summ.get('critic') or 'none recorded'}"
    )
    for p in st["problems"][:10]:
        out.append(f"    · {p}")

    out += ["", "-- discovered tests (the artifact, not the prose) --"]
    if disc["strong"]:
        for f in disc["strong"]:
            out.append(f"  strong  {f}  [{', '.join(disc['markers'].get(f, [])) or 'no markers'}]")
    for f in disc["weak"][:5]:
        out.append(f"  weak    {f}")
    if not disc["strong"] and not disc["weak"]:
        out.append("  (none — this feature's verification runs through an operator CLI)")
    out.append(f"  method proposed from TESTS ALONE: {proposed or 'UNDETERMINED'} — {why}")
    out.append(
        "    ^ this reads the markers, not the acceptance criterion above. A feature\n"
        "      whose tests are all fixtures still needs a live run when its AC names a\n"
        "      real gateway. Read both before changing verification_method."
    )

    note = resume_block(fid)
    if note:
        out += ["", "-- the session note's own Resume / next --"]
        out += ["  " + line for line in note.splitlines()]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
def cmd_list(args) -> int:
    queue = build_queue()
    if args.json:
        print(json.dumps({"rows": queue["rows"], "cycles": queue["cycles"]}, indent=2))
    elif args.markdown:
        print(_render_markdown(queue, find_drift(queue)))
    else:
        print(_render_text(queue))
    return 0


def cmd_show(args) -> int:
    queue = build_queue()
    text = _render_show(args.id, queue)
    print(text)
    return 2 if text.startswith("✗") else 0


def cmd_check(args) -> int:
    queue = build_queue()
    drift = find_drift(queue)
    actionable = [f for f in drift if f["severity"] != "low"]
    if args.json:
        print(json.dumps(drift, indent=2))
    elif not drift:
        print("✓ nothing has drifted; no decision is waiting on you")
    else:
        for f in drift:
            print(f"[{f['severity'].upper():6}] {f['id']}: {f['detail']}")
            print(f"           → {f['action']}")
    return 1 if actionable else 0


def _leases_are_active() -> list:
    runtime = agent_pool.load_runtime()
    import time as _time

    now = _time.time()
    return [fid for fid, lease in runtime["leases"].items() if agent_pool.lease_active(lease, now)]


def cmd_run(args) -> int:
    """Drive the machine-runnable steps through the recorder, and STOP at the rest.

    What this does NOT do is as important as what it does. It never calls
    ``evidence.py record`` on a step it could not execute: a hand-written record
    the tool invented would satisfy the human-attestation path with nobody's
    attestation, which is the self-granted `--verified` defect wearing a new hat.
    Unrunnable steps are printed and left empty.
    """
    fid = args.id
    features = agent_pool.load_features(fetch=False)
    if not any(f["id"] == fid for f in features):
        print(f"✗ unknown feature id: {fid}", file=sys.stderr)
        return 2

    # The e2e and integration lanes bind the dashboard, docker, and the data tiers;
    # the live lane binds the IB ports. None of that is shareable, and a sibling
    # holding a lease means somebody may be mid-run (AGENTS.md "No parallel
    # integration/live tests"; broker-and-live rule 24).
    held = _leases_are_active()
    if held and not args.force:
        print(
            f"✗ {len(held)} active lease(s): {', '.join(sorted(held))}.\n"
            "  This lane needs the whole box — the dashboard port, docker, the data\n"
            "  tiers, and the IB ports are all single-occupancy. Wait for them to\n"
            "  integrate, or re-run with --force if you know those sessions are dead.",
            file=sys.stderr,
        )
        return 1

    steps = evidence.feature_steps(fid, features)
    disc = discover_tests(fid)
    # The selection is grep-derived and therefore FALLIBLE — MD-003's "strong"
    # matches include two harness tests that merely mention it as an example id.
    # Executing an unrelated passing suite and filing it as a feature's evidence is
    # the failure evidence.reexecute explicitly says nothing mechanical can catch
    # ("it does NOT establish that the commands are RELEVANT"). So the plan is
    # always printed, --yes is required to run it, and --only overrides it outright.
    selected = list(args.only) if args.only else list(disc["strong"])
    plan = []
    if steps:
        plan.append((1, ["./init.sh"], "step 1 is the same for every feature"))
    if len(steps) > 1 and selected:
        plan.append(
            (
                2,
                [".venv/bin/python", "-m", "pytest", *selected, "-q"],
                f"{len(selected)} test file(s) "
                + ("named with --only" if args.only else "that name this feature (grep-derived)"),
            )
        )

    print(f"== verification lane: {fid}")
    print(f"   {len(plan)} of {len(steps)} step(s) can be executed here.\n")
    for n, argv, why in plan:
        print(f"  step {n}: {' '.join(argv)}\n           ({why})")
    unrunnable = [n for n in range(1, len(steps) + 1) if n not in {p[0] for p in plan}]
    if unrunnable:
        print(f"\n  steps {unrunnable} cannot be executed by this tool:")
        for n in unrunnable:
            print(f"    {n}. {steps[n - 1][:100]}")
        print(
            "  These are the acceptance criterion and the evidence instruction — a\n"
            "  judgement and a live observation. They are left UNRECORDED on purpose.\n"
            f"  See: tools/verify_queue.py show {fid}"
        )
    if args.dry_run or not args.yes:
        print(
            "\n[nothing executed] The step-2 selection above is grep-derived and this\n"
            "  tool cannot tell whether those tests actually exercise the acceptance\n"
            "  criterion — only that they name the feature. Read it, then:\n"
            f"    tools/verify_queue.py run {fid} --yes"
            + ("" if args.only else "            (or --only <paths> to choose them yourself)")
        )
        return 0

    env = dict(os.environ)
    if args.e2e:
        env["ATP_RUN_E2E"] = "1"
    if args.integration:
        env["ATP_RUN_INTEGRATION"] = "1"

    print()
    failed = 0
    for n, argv, _why in plan:
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "evidence.py"),
                "run",
                fid,
                "--step",
                str(n),
                *argv,
            ],
            cwd=str(ROOT),
            env=env,
            check=False,
        )
        if proc.returncode != 0:
            failed += 1
            if not args.keep_going:
                print(
                    f"✗ step {n} failed; stopping (use --keep-going to continue)", file=sys.stderr
                )
                break
    st = evidence_state(fid, features)
    print(
        f"\n== record now: {st['summary'].get('steps_evidenced', 0)}/"
        f"{st['summary'].get('steps_total', len(steps))} steps"
    )
    for p in st["problems"][:10]:
        print(f"   · {p}")
    return 1 if failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list", help="the ranked worklist")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--markdown", action="store_true", help="issue-body form, with drift first")

    sh = sub.add_parser("show", help="everything needed to verify one feature")
    sh.add_argument("id")

    ck = sub.add_parser(
        "check", help="report only what drifted; exit 1 if anything needs a decision"
    )
    ck.add_argument("--json", action="store_true")

    rn = sub.add_parser("run", help="execute the machine-runnable steps (needs the whole box)")
    rn.add_argument("id")
    rn.add_argument(
        "--yes",
        action="store_true",
        help="execute the plan. Without it, run only PRINTS what it would do — the "
        "step-2 test selection is grep-derived and a human must look at it before it "
        "becomes a feature's evidence.",
    )
    rn.add_argument(
        "--only",
        nargs="+",
        default=[],
        metavar="PATH",
        help="use these test paths for step 2 instead of the discovered selection",
    )
    rn.add_argument("--dry-run", action="store_true", help="print the plan, execute nothing")
    rn.add_argument("--e2e", action="store_true", help="set ATP_RUN_E2E=1")
    rn.add_argument("--integration", action="store_true", help="set ATP_RUN_INTEGRATION=1")
    rn.add_argument("--keep-going", action="store_true", help="do not stop at the first failure")
    rn.add_argument("--force", action="store_true", help="run despite active sibling leases")

    args = ap.parse_args(argv)
    return {"list": cmd_list, "show": cmd_show, "check": cmd_check, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
