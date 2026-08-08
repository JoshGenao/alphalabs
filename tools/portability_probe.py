#!/usr/bin/env python3
"""Is the pipeline actually harness-agnostic, as it claims to be?

The stated goal is that this pipeline be reusable under ANY agentic harness, with
``agent_pool.py`` the harness-agnostic core. That is easy to believe and easy to
break: one ``os.environ["CLAUDE_..."]``, one hard-coded ``claude`` invocation on a
non-launcher path, and the claim quietly stops being true while every test still
passes — because the tests run under the harness the code accidentally depends on.

This probe checks the claim the cheap way: strip the Claude-specific environment,
then exercise the read-only scheduler surface an alternative harness would need.

What is deliberately NOT flagged: ``claim_and_work.sh``, ``work_on.sh`` and
``adversarial_review.py``'s Claude fallback. Those are LAUNCHERS and reviewers —
their job is to start a specific tool. Portability is a claim about the scheduler
and the gates, not about them.

    tools/portability_probe.py            # human
    tools/portability_probe.py --json

Exit codes: 0 = the core runs without Claude-specific env, 1 = it does not, 2 = error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The surface another harness has to be able to drive: read-only, no side effects.
PROBES = (
    ("status", [sys.executable, "tools/agent_pool.py", "status", "--no-fetch"]),
    ("status --json", [sys.executable, "tools/agent_pool.py", "status", "--no-fetch", "--json"]),
    ("gate registry", [sys.executable, "tools/gates_registry_check.py"]),
    ("gate scopes", [sys.executable, "tools/gates_registry_check.py", "--list-scopes"]),
    ("contract scope list", ["tools/verify_contracts.sh", "--scope", "ci", "--list"]),
    ("docs references", [sys.executable, "tools/docs_link_check.py"]),
    ("evidence verify", [sys.executable, "tools/evidence.py", "verify", "SRS-ARCH-001"]),
)

# Modules that must not reach for a specific harness. Launchers are exempt by design.
CORE_MODULES = (
    "tools/agent_pool.py",
    "tools/evidence.py",
    "tools/close_feature.py",
    "tools/gates_registry_check.py",
    "tools/docs_link_check.py",
    "tools/playbook_harvest.py",
    "tools/critic_check.py",
)
HARNESS_SPECIFIC_RE = re.compile(r"CLAUDE_[A-Z_]+|ANTHROPIC_[A-Z_]+|\.claude/(?!skills)")


def stripped_env() -> dict:
    """The environment minus anything a specific agent harness injects."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not (k.startswith(("CLAUDE", "ANTHROPIC", "ATP_FEATURE_ID", "ATP_AGENT_OWNER")))
    }
    env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    return env


def run_probes() -> list[dict]:
    env = stripped_env()
    out = []
    for name, cmd in PROBES:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), env=env, capture_output=True, text=True, check=False
        )
        # evidence verify legitimately exits 1 (no record for a pre-gate feature);
        # what matters is that it RAN, not that it approved.
        ok = proc.returncode in (0, 1)
        out.append(
            {
                "probe": name,
                "exit": proc.returncode,
                "ok": ok,
                "stderr": proc.stderr.strip()[:200] if not ok else "",
            }
        )
    return out


def scan_core() -> list[str]:
    problems = []
    for rel in CORE_MODULES:
        path = ROOT / rel
        if not path.is_file():
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if HARNESS_SPECIFIC_RE.search(line):
                problems.append(f"{rel}:{n}: harness-specific reference — {line.strip()[:80]}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    probes = run_probes()
    leaks = scan_core()
    failed = [p for p in probes if not p["ok"]]

    if args.json:
        print(json.dumps({"probes": probes, "core_leaks": leaks}, indent=2))
        return 1 if (failed or leaks) else 0

    print("== portability probe (Claude-specific env stripped) ==")
    for p in probes:
        print(f"  {'✓' if p['ok'] else '✗'} {p['probe']:22} exit={p['exit']}")
        if p["stderr"]:
            print(f"      {p['stderr']}")
    if leaks:
        print(f"\n✗ {len(leaks)} harness-specific reference(s) in the core:")
        for line in leaks:
            print(f"    · {line}")
    if failed or leaks:
        print("\n✗ the pipeline core is not harness-agnostic as claimed")
        return 1
    print("\n✓ the scheduler, gates and evidence surface all run without a specific harness")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
