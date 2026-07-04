#!/usr/bin/env python3
"""adversarial_review.py — the judgment-critic pass with a Codex→Claude failover.

The judgment pass (Layer 2 of the critic) wants a *fresh set of eyes* on the diff:
a reviewer that never saw the implementation conversation and defaults to
skepticism. Codex is the primary reviewer, but its usage limit regularly
bottlenecks the whole project — and the old auto-fallback in the coding prompt
only triggered on ``{"verdict":"error"}`` (missing node/companion), never on the
actual rate-limit case. So every limit hit forced a *manual* substitution.

This dispatcher fixes that. It:

1. **Predicts** Codex availability from a local cooldown cache + the openai-codex
   plugin's own job state (``~/.claude/plugins/data/codex-openai-codex/state/*/
   state.json``). If a recent adversarial-review job ``failed`` with a "you've hit
   your usage limit … try again at H:MM" summary whose reset is still in the
   future, it skips Codex outright.
2. Otherwise **runs Codex** via ``tools/codex_review.sh`` (which now emits ``--json``).
3. **Detects** a runtime usage-limit (non-zero exit + a usage-limit phrase, or an
   ``{"verdict":"error"}`` payload), records the reset to the cooldown cache, and
4. **Falls back** to a fresh-context Claude reviewer: ``git diff BASE...HEAD`` piped
   to ``claude -p`` with ``prompts/critic_prompt.md`` and an independence system
   prompt, in read-only plan mode — the diff is the only evidence, no build chat.
5. **Normalizes** every reviewer to one canonical verdict (``block|warn|approve``)
   and tags the result with which reviewer ran.

Usage:
    tools/adversarial_review.py [BASE_REF]     # default BASE_REF = origin/main
    tools/adversarial_review.py --status       # is Codex available? until when?
    tools/adversarial_review.py --force-claude # skip Codex (testing / known-down)

Exit code: 0 on approve/warn, 1 on block, 2 on a usage error (matches the
block-halts-you contract in prompts/coding_prompt.md).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CRITIC_PROMPT = REPO_ROOT / "prompts" / "critic_prompt.md"
CODEX_REVIEW = REPO_ROOT / "tools" / "codex_review.sh"
COOLDOWN_FILE = REPO_ROOT / "tools" / ".codex_cooldown.json"
PLUGIN_STATE_DIR = Path.home() / ".claude" / "plugins" / "data" / "codex-openai-codex" / "state"

USAGE_LIMIT_RE = re.compile(r"usage limit|rate limit|hit your (?:usage|rate) limit", re.IGNORECASE)
RESET_RE = re.compile(r"try again at\s+(\d{1,2}):(\d{2})\s*([AaPp][Mm])")

FRESH_EYES_SYSTEM = (
    "You are an INDEPENDENT adversarial code reviewer. You did NOT write this code "
    "and have never seen the author's reasoning or conversation. The diff piped to "
    "you is the only evidence — the author's claims are not evidence. Default to "
    "skepticism and actively try to construct a failing input, race, or "
    "safety-invariant violation. You may read the repository (read-only) for "
    "context, but never treat the absence of a counterexample as proof of "
    "correctness: 'approve' is permitted ONLY if you genuinely tried and failed to "
    "break it. Output ONLY the JSON verdict object described in the prompt."
)


# ----------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/unit/test_adversarial_review.py)
# ----------------------------------------------------------------------------
def is_rate_limited(output: str, exit_code: int) -> bool:
    """True if a Codex invocation's result means "usage/rate limit hit".

    Covers the real runtime case the old trigger missed: a non-zero exit whose
    output carries a usage-limit phrase. Also treats the precondition
    ``{"verdict":"error"}`` payload as "Codex unavailable → fall back".
    """
    text = output or ""
    if USAGE_LIMIT_RE.search(text):
        return True
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            if json.loads(stripped).get("verdict") == "error":
                return True
        except (json.JSONDecodeError, AttributeError):
            pass
    return False


def parse_reset_time(summary: str, hit_at: datetime | None = None) -> datetime | None:
    """Parse "…try again at 1:35 PM" into an absolute local datetime.

    ``hit_at`` is when the limit was hit (a tz-aware local datetime); the reset
    clock time is resolved on that date, rolling to the next day if it would land
    before the hit. Returns None if the summary has no reset hint.
    """
    hit_at = hit_at or datetime.now().astimezone()
    m = RESET_RE.search(summary or "")
    if not m:
        return None
    hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ap == "pm" and hh != 12:
        hh += 12
    if ap == "am" and hh == 12:
        hh = 0
    reset = hit_at.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if reset <= hit_at:
        reset += timedelta(days=1)
    return reset


def normalize_verdict(payload: dict, reviewer: str) -> dict:
    """Map any reviewer's payload to the canonical block|warn|approve schema.

    Codex's plugin schema uses ``approve|needs-attention``; the Claude fallback
    follows critic_prompt.md and already emits ``block|warn|approve``. For
    ``needs-attention`` we escalate to ``block`` when any finding is
    critical/high severity, else ``warn`` (never silently ``approve``).
    """
    raw = str(payload.get("verdict", "")).strip().lower()
    findings = payload.get("findings") or []
    severities = {
        str(f.get("severity", "")).strip().lower() for f in findings if isinstance(f, dict)
    }

    if raw in ("block", "warn", "approve"):
        verdict = raw
    elif raw == "needs-attention":
        verdict = "block" if severities & {"critical", "high", "block"} else "warn"
    else:
        # Unknown/empty verdict from a reviewer we can't read → fail closed.
        verdict = "block"
    return {
        "verdict": verdict,
        "reviewer": reviewer,
        "summary": payload.get("summary", ""),
        "findings": findings,
        "next_steps": payload.get("next_steps", []),
    }


def _verdict_from_envelope(obj: dict) -> dict | None:
    """Dig the canonical verdict object out of a parsed reply.

    The Claude fallback (and older Codex builds) emit a bare object with
    ``verdict`` at the top level. The Codex companion's ``--json`` mode instead
    wraps it in an envelope whose top-level keys are ``review/target/context/
    codex/result/rawOutput/…`` — the parsed verdict is at ``obj["result"]`` and
    the raw reviewer text is duplicated as a JSON *string* in
    ``obj["rawOutput"]`` and ``obj["codex"]["stdout"]``. Without this unwrap the
    envelope parses fine as JSON but has no top-level ``verdict``, so it looks
    "unparseable" and Codex's verdict is silently dropped for the fallback.
    """
    if "verdict" in obj:
        return obj
    result = obj.get("result")
    if isinstance(result, dict) and "verdict" in result:
        return result
    raw = obj.get("rawOutput")
    if isinstance(raw, str) and (inner := extract_json(raw)):
        return inner
    codex = obj.get("codex")
    if isinstance(codex, dict) and isinstance(codex.get("stdout"), str):
        if inner := extract_json(codex["stdout"]):
            return inner
    return None


def extract_json(text: str) -> dict | None:
    """Pull the JSON verdict object out of an LLM's (possibly prose-wrapped) reply."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidates.append(text[first : last + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and (verdict_obj := _verdict_from_envelope(obj)):
            return verdict_obj
    return None


# ----------------------------------------------------------------------------
# Cooldown prediction (I/O)
# ----------------------------------------------------------------------------
def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def cooldown_from_cache(now: datetime | None = None) -> datetime | None:
    """Return a future reset time recorded by a prior limit hit, else None."""
    now = now or datetime.now().astimezone()
    data = _read_json(COOLDOWN_FILE)
    if not data or not data.get("until"):
        return None
    try:
        until = datetime.fromisoformat(data["until"])
    except ValueError:
        return None
    return until if until > now else None


def cooldown_from_plugin_state(state_dir: Path | None = None, now: datetime | None = None):
    """Inspect the plugin's own job state for the most recent adversarial review.

    If the newest review job across all worktrees ``failed`` with a usage-limit
    summary whose reset is still in the future, return that reset (Codex is
    account-wide limited). A newer non-limited job means it has since recovered.
    """
    state_dir = state_dir or PLUGIN_STATE_DIR
    now = now or datetime.now().astimezone()
    if not state_dir.is_dir():
        return None
    newest = None
    for sf in state_dir.glob("*/state.json"):
        data = _read_json(sf)
        for job in (data or {}).get("jobs", []):
            if job.get("kind") != "adversarial-review":
                continue
            stamp = job.get("updatedAt") or job.get("completedAt") or ""
            if newest is None or stamp > newest.get("_stamp", ""):
                newest = {**job, "_stamp": stamp}
    if not newest or newest.get("status") != "failed":
        return None
    if not USAGE_LIMIT_RE.search(newest.get("summary", "")):
        return None
    try:
        hit_at = datetime.fromisoformat(
            (newest.get("completedAt") or newest.get("_stamp")).replace("Z", "+00:00")
        ).astimezone()
    except (ValueError, AttributeError):
        hit_at = now
    reset = parse_reset_time(newest.get("summary", ""), hit_at)
    return reset if reset and reset > now else None


def codex_cooldown_until(now: datetime | None = None) -> datetime | None:
    """Best estimate of when Codex becomes usable again, or None if available."""
    now = now or datetime.now().astimezone()
    return cooldown_from_cache(now) or cooldown_from_plugin_state(now=now)


def record_cooldown(summary: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now().astimezone()
    reset = parse_reset_time(summary, now) or (now + timedelta(hours=1))
    try:
        COOLDOWN_FILE.write_text(
            json.dumps({"until": reset.isoformat(), "summary": summary[:300]}) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    return reset


# ----------------------------------------------------------------------------
# Reviewers (I/O)
# ----------------------------------------------------------------------------
def run_codex(base_ref: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["bash", str(CODEX_REVIEW), base_ref],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def run_claude_fallback(base_ref: str, timeout: int = 900) -> tuple[int, str]:
    diff = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", f"{base_ref}...HEAD"],
        text=True,
        capture_output=True,
    ).stdout
    prompt = CRITIC_PROMPT.read_text(encoding="utf-8")
    proc = subprocess.run(
        [
            "claude",
            "-p",
            prompt,
            "--permission-mode",
            "plan",
            "--model",
            "opus",
            "--append-system-prompt",
            FRESH_EYES_SYSTEM,
        ],
        cwd=str(REPO_ROOT),
        input=diff,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "")


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
def emit(result: dict) -> int:
    """Print the canonical verdict JSON + a human reviewer line; return exit code."""
    print(json.dumps(result, indent=2))
    note = f"reviewer: {result['reviewer']}"
    if result.get("reviewer_note"):
        note += f" ({result['reviewer_note']})"
    print(note, file=sys.stderr)
    return 1 if result["verdict"] == "block" else 0


def review(base_ref: str, *, force_claude: bool = False) -> dict:
    cooldown = None if force_claude else codex_cooldown_until()
    if force_claude or cooldown:
        note = "forced" if force_claude else f"codex limited until {cooldown:%-I:%M %p}"
        return _claude(base_ref, note)

    code, out = run_codex(base_ref)
    if is_rate_limited(out, code):
        reset = record_cooldown(out)
        note = f"codex limited until {reset:%-I:%M %p}" if reset else "codex usage limit"
        return _claude(base_ref, note)

    payload = extract_json(out)
    if payload is None:
        # Codex ran but we couldn't read a verdict — try the fresh-eyes reviewer
        # rather than guessing. (Not a limit, so no cooldown recorded.)
        return _claude(base_ref, "codex output unparseable")
    return normalize_verdict(payload, "codex")


def _claude(base_ref: str, note: str) -> dict:
    try:
        code, out = run_claude_fallback(base_ref)
    except subprocess.TimeoutExpired:
        return {
            "verdict": "block",
            "reviewer": "claude-fallback",
            "reviewer_note": f"{note}; fallback timed out",
            "summary": "Fresh-context Claude reviewer timed out — treat as BLOCK.",
            "findings": [],
        }
    except FileNotFoundError:
        # Neither reviewer is available — fail closed so the agent halts rather
        # than silently proceeding without a judgment pass.
        return {
            "verdict": "block",
            "reviewer": "none",
            "reviewer_note": f"{note}; `claude` CLI not found — run the review manually",
            "summary": "No reviewer available (Codex down and claude CLI missing) — BLOCK.",
            "findings": [],
        }
    payload = extract_json(out) or {"verdict": "block", "summary": out[:500]}
    result = normalize_verdict(payload, "claude-fallback")
    result["reviewer_note"] = note
    return result


def cmd_status() -> int:
    until = codex_cooldown_until()
    if until:
        print(f"Codex limited until {until:%-I:%M %p %Z} — reviews will use the Claude fallback.")
    else:
        print("Codex available.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Adversarial review with Codex→Claude failover.")
    ap.add_argument("base_ref", nargs="?", default="origin/main")
    ap.add_argument("--status", action="store_true", help="report Codex availability and exit")
    ap.add_argument(
        "--force-claude", action="store_true", help="skip Codex, use the Claude reviewer"
    )
    args = ap.parse_args()

    if args.status:
        return cmd_status()
    if not CRITIC_PROMPT.is_file():
        print(json.dumps({"verdict": "error", "reason": f"missing {CRITIC_PROMPT}"}))
        return 2
    return emit(review(args.base_ref, force_claude=args.force_claude))


if __name__ == "__main__":
    raise SystemExit(main())
