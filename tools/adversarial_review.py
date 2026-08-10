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
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CRITIC_PROMPT = REPO_ROOT / "prompts" / "critic_prompt.md"
CODEX_REVIEW = REPO_ROOT / "tools" / "codex_review.sh"
COOLDOWN_FILE = REPO_ROOT / "tools" / ".codex_cooldown.json"
PLUGIN_STATE_DIR = Path.home() / ".claude" / "plugins" / "data" / "codex-openai-codex" / "state"

USAGE_LIMIT_RE = re.compile(r"usage limit|rate limit|hit your (?:usage|rate) limit", re.IGNORECASE)

# One vocabulary for "severe enough to stop the agent". It was written twice — as a
# literal inside normalize_verdict and as this constant 236 lines later — so the two
# could drift silently and disagree about the same finding.
BLOCKING_SEVERITIES = {"block", "critical", "high"}
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
        verdict = "block" if severities & BLOCKING_SEVERITIES else "warn"
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


def outcome(payload: dict | None, raw: str, reviewer: str) -> dict:
    """THE decision: did this reviewer invocation actually judge the diff?

    Every entry point routes through here. Patching the sites one at a time is what
    made rounds 2-6 of this branch: the "class fix" in the previous commit covered
    four call sites and missed three, and the reviewer found all three. The sites
    are not the class — the QUESTION is, so it gets asked in exactly one place.

    No verdict, and therefore an `attempt`, when:
      · nothing verdict-bearing could be parsed (`payload is None`);
      · the payload is an explicit error envelope (`{"verdict": "error"}`), which
        `is_rate_limited` already treats as "reviewer unavailable, fail over" — the
        same envelope must not be an outage to one function and a legitimate BLOCK
        to another;
      · the raw output carries a usage/rate-limit phrase.

    Fails closed in every one of those cases (verdict `block`, exit 1), because the
    agent must halt when nobody judged the diff. What changes is only whether the
    ledger CALLS it a round.
    """
    if payload is None:
        return {
            "verdict": "block",
            "reviewer": reviewer,
            "summary": unreadable_reason(raw),
            "findings": [],
            "no_verdict": True,
        }
    if str(payload.get("verdict") or "").strip().lower() == "error" or is_rate_limited(raw, 0):
        return {
            "verdict": "block",
            "reviewer": reviewer,
            "summary": str(payload.get("reason") or payload.get("summary") or "")[:300]
            or unreadable_reason(raw),
            "findings": [],
            "no_verdict": True,
        }
    return normalize_verdict(payload, reviewer)


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
    # ATP_REVIEW_DISPATCHED tells codex_review.sh that THIS process will record the
    # round, so it must not record it too. Without the flag a dispatched round would
    # land in review.jsonl twice and inflate every count derived from it.
    env = {**os.environ, "ATP_REVIEW_DISPATCHED": "1"}
    proc = subprocess.run(
        ["bash", str(CODEX_REVIEW), base_ref],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        env=env,
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
# The two reviewers describe a finding differently, and the first version of this
# module only understood one of them. A real Codex round recorded rules=['?'] for
# every finding and blocking_rules=[] — silently gutting the promotion-candidate
# mining that is the whole reason the rule ids are stored.
#
#   Claude fallback : {"rule": "meta:critic-self-modification", "severity": "block"}
#   Codex           : {"title": "Missing feature/SRS trace", "severity": "high"}
#
# Neither is wrong; the record has to speak both. Same normalization the verdict
# itself already gets in normalize_verdict().
def findings_list(payload: object) -> list:
    """Whatever a reviewer put in `findings`, as a list — for counting and grouping.

    Not just the ENTRIES need normalizing but the CONTAINER: a reviewer emitting
    `findings: "some prose"` made `len()` report 13 for "totally wrong" (a string
    is iterable, so it counted characters) and grouping iterate character by
    character. A malformed container is ONE unknown finding, not thirteen and not
    zero — it must not read as "the reviewer found nothing" (CLAUDE.md rule 3).
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, tuple):
        return list(payload)
    return [payload]  # a dict, a string, a number: one thing we could not read


def finding_rule(finding: object) -> str:
    """A stable-ish grouping key for a finding, whichever reviewer produced it.

    Takes `object`, not `dict`: nothing validates a reviewer's payload shape before
    it gets here. `normalize_verdict` already filters with `isinstance(f, dict)`
    while this did not, so a reviewer emitting `findings: ["some string"]` passed
    normalization and then killed the telemetry append with an AttributeError.
    A malformed finding is UNKNOWN ("?"), never dropped (CLAUDE.md rule 3).
    """
    if not isinstance(finding, dict):
        return "?"
    for key in ("rule", "title", "id", "type"):
        value = str(finding.get(key) or "").strip()
        if value:
            # Titles are prose; lower-case and clip so the same class groups across
            # rounds without turning the ledger into paragraphs.
            return value[:80].lower()
    return "?"


def is_blocking(finding: object) -> bool:
    """Is this finding severe enough to block? A malformed one is not evidence of safety.

    Fails toward "not blocking" only because the VERDICT (which `normalize_verdict`
    computes independently, and which defaults to `block` on anything unreadable)
    is what gates the agent — this feeds the ledger's `blocking_rules` list, not the
    exit code.
    """
    if not isinstance(finding, dict):
        return False
    return str(finding.get("severity") or "").strip().lower() in BLOCKING_SEVERITIES


def unreadable_reason(raw: str) -> str:
    """The reviewer's own words about why its output could not be read.

    A Codex failure envelope keeps the real cause somewhere extract_json never
    looks — `parseError`, `codex.stderr`, or a bare `reason` — so a round recorded
    as merely "unparsable" would throw away the one detail that makes it
    actionable, usually "you've hit your usage limit".
    """
    text = (raw or "").strip()
    try:
        obj = json.loads(text) if text.startswith("{") else {}
    except json.JSONDecodeError:
        obj = {}
    codex = obj.get("codex") if isinstance(obj.get("codex"), dict) else {}
    for candidate in (
        obj.get("parseError"),
        obj.get("reason"),
        obj.get("error"),
        codex.get("stderr"),
        codex.get("stdout"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return f"unreadable reviewer output: {candidate.strip()[:300]}"
    return f"unreadable reviewer output: {text[:300] or '(empty)'}"


ATTEMPT_KIND = "attempt"
ROUND_KIND = "round"


def is_round(rec: dict) -> bool:
    """Does this record count toward `Adversarial rounds:`?

    A ROUND is a review pass that ends in a verdict. An ATTEMPT is a reviewer
    invocation that produced none — a rate-limited Codex, an unreadable envelope —
    after which the dispatcher fell back and someone else delivered the verdict.
    One round can contain several attempts; counting attempts as rounds would
    inflate the only falsifiable number the playbook system publishes, and
    CLAUDE.md rule 7 is explicit that an availability failure is not a verdict.

    A record written before `kind` existed is a ROUND: absent here means "old",
    not "attempt", and defaulting the other way would silently rewrite history
    down to zero for every feature already on main.
    """
    return str(rec.get("kind") or ROUND_KIND) != ATTEMPT_KIND


def read_records(path: Path) -> list[dict] | None:
    """Every record in a review.jsonl, or None if it cannot be read.

    None means UNKNOWN and is never [] (CLAUDE.md rule 3): a truncated write or a
    permissions error must not present as "this feature was never reviewed". Callers
    decide what unknown means for them — every current one declines to judge.

    NEVER raises, for the same reason the writer never does. Guarding only the write
    path left the READ path able to kill the gate: `emit()` calls `count_rounds` after
    printing its verdict, and a torn write mid-multibyte-character raises
    UnicodeDecodeError — a ValueError, which `except OSError` does not catch. The
    reader of a telemetry file is as load-bearing as its writer.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)  # a bad line means the file is untrustworthy
            if isinstance(rec, dict):
                out.append(rec)
        return out
    except Exception:  # noqa: BLE001 — unknown, never a confident answer; see docstring
        return None


def count_rounds(path: Path) -> int | None:
    """How many verdict-bearing rounds this feature recorded; None if unreadable."""
    recs = read_records(path)
    return None if recs is None else sum(1 for r in recs if is_round(r))


def attempt_record(reviewer: str, note: str, summary: str) -> dict:
    """A reviewer invocation that produced no verdict.

    Recorded so the ledger shows the failover happened. Before this, `run_codex`
    set ATP_REVIEW_DISPATCHED=1 (so the shell would not double-record), then a
    rate-limited Codex fell through to `_claude` and only the FALLBACK was
    recorded — the failed attempt left no trace on the one path the docs tell
    agents to use. Same shape as a round so a reader needs one parser, but
    `kind` keeps it out of every count.
    """
    return {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "kind": ATTEMPT_KIND,
        "reviewer": reviewer,
        # Not "block": an attempt has no opinion about the diff, and a reader
        # scanning for blocks must not find one here.
        "verdict": "none",
        "n_findings": 0,
        "rules": [],
        "blocking_rules": [],
        "reviewer_note": note,
        # Already-formed prose passes through: running a clear "no reviewer
        # available" summary back through unreadable_reason() prefixed it with
        # "unreadable reviewer output:", which describes the wrong failure.
        "summary": (summary.strip() or f"{reviewer}: {note}")[:300],
    }


def round_record(result: dict) -> dict:
    """The structured form of one review round.

    ``Adversarial rounds:`` is the only falsifiable claim the playbook system makes —
    ``prompts/coding_prompt.md`` calls it "a measurement, not decoration" — and it
    appeared in 1 of 38 session notes, because it was prose an agent had to remember
    to write. The reviewer knows the number; it should not be asking anyone to
    recount it.
    """
    findings = findings_list(result.get("findings"))
    return {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "kind": ROUND_KIND,
        "reviewer": result.get("reviewer", "?"),
        "verdict": result.get("verdict", "?"),
        "n_findings": len(findings),
        # The rule ids are what P2-4 mines for defect classes worth promoting into
        # tools/critic_check.py — the loop the playbooks currently run by hand.
        "rules": sorted({finding_rule(f) for f in findings}),
        "blocking_rules": sorted({finding_rule(f) for f in findings if is_blocking(f)}),
        "reviewer_note": result.get("reviewer_note", ""),
        # The reviewer's own account of the round. For an unreadable one this is the
        # only actionable content there is ("you've hit your usage limit…"), and
        # recording the round without it would preserve the count while discarding
        # the reason.
        "summary": str(result.get("summary") or "")[:300],
    }


def append_round(result: dict, fid: str | None = None) -> Path | None:
    """Append this round to .harness/runs/<fid>/review.jsonl. Never fails the review.

    Telemetry that can break the gate it measures would be worse than no telemetry:
    an agent whose review dies because a log directory is read-only learns to stop
    running the review.
    """
    return _append(lambda: _record_for(result), fid)


def _record_for(result: dict) -> dict:
    """A result flagged `no_verdict` blocks the agent but records as an ATTEMPT.

    One funnel, so a future no-verdict path cannot become a round by forgetting to ask.
    """
    if result.get("no_verdict"):
        return attempt_record(
            str(result.get("reviewer") or "?"),
            str(result.get("reviewer_note") or ""),
            str(result.get("summary") or ""),
        )
    return round_record(result)


def append_attempt(reviewer: str, note: str, raw: str, fid: str | None = None) -> Path | None:
    """Record a reviewer invocation that produced no verdict. Never fails the review.

    ``raw`` is the reviewer's unparsed output, so the cause is dug out of it here.
    """
    return _append(lambda: attempt_record(reviewer, note, unreadable_reason(raw)), fid)


def _append(build: Callable[[], dict], fid: str | None = None) -> Path | None:
    """Build and append one record, or return None. NEVER raises.

    `build` is a callable, not a dict, so that BUILDING the record happens inside
    the guard too. It used to happen at the call site: a reviewer emitting
    `findings: ["a bare string"]` passed normalize_verdict (which filters on
    `isinstance(f, dict)`) and then died in finding_rule with an AttributeError —
    after the verdict had already been printed, killing the review that had
    successfully completed. The docstring promised "never fails the review" while
    only `OSError` was caught.

    So the except is deliberately broad. Telemetry is strictly observational here;
    the verdict and the exit code are computed before it and do not depend on it.
    A ledger that can kill the gate it measures teaches agents to stop running the
    gate — the one outcome worse than having no ledger.
    """
    fid = fid or os.environ.get("ATP_FEATURE_ID", "")
    if not fid:
        return None
    try:
        path = REPO_ROOT / ".harness" / "runs" / fid / "review.jsonl"
        line = json.dumps(build(), sort_keys=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 — see docstring; must never fail the review
        print(f"(could not record review telemetry: {exc!r})", file=sys.stderr)
        return None
    return path


def emit(result: dict) -> int:
    """Print the canonical verdict JSON + a human reviewer line; return exit code."""
    print(json.dumps(result, indent=2))
    note = f"reviewer: {result['reviewer']}"
    if result.get("reviewer_note"):
        note += f" ({result['reviewer_note']})"
    recorded = append_round(result)
    if recorded:
        # append_round is wrapped so telemetry can never fail the review it measures;
        # re-opening the file here outside any guard reintroduced exactly that, and
        # leaked the handle. Count inside the same protection.
        n = count_rounds(recorded)
        if n is None:
            note += " · telemetry unreadable"
        elif result.get("no_verdict"):
            # Say what was actually written. Reporting "round N" for an attempt is
            # how the count and the note drift apart in the first place.
            note += f" · attempt recorded (no verdict; still {n} round(s))"
        else:
            note += f" · round {n} recorded"
    print(note, file=sys.stderr)
    return 1 if result["verdict"] == "block" else 0


def review(base_ref: str, *, force_claude: bool = False) -> dict:
    cooldown = None if force_claude else codex_cooldown_until()
    if force_claude or cooldown:
        note = "forced" if force_claude else f"codex limited until {cooldown:%-I:%M %p}"
        return _claude(base_ref, note)

    code, out = run_codex(base_ref)
    # Every path below that abandons Codex must record the attempt first. run_codex
    # set ATP_REVIEW_DISPATCHED=1, so codex_review.sh deliberately did NOT record —
    # if we also say nothing, a rate-limited Codex vanishes and the ledger claims the
    # fallback was the only reviewer ever asked.
    if is_rate_limited(out, code):
        reset = record_cooldown(out)
        note = f"codex limited until {reset:%-I:%M %p}" if reset else "codex usage limit"
        append_attempt("codex", note, out)
        return _claude(base_ref, note)

    payload = extract_json(out)
    if payload is None:
        # Codex ran but we couldn't read a verdict — try the fresh-eyes reviewer
        # rather than guessing. (Not a limit, so no cooldown recorded.)
        append_attempt("codex", "codex output unparseable", out)
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
            # Blocks the agent (exit 1) but is NOT a round: CLAUDE.md rule 7 —
            # "a reviewer TIMEOUT is not a verdict … retry it". Counting it would
            # let an availability failure satisfy `Adversarial rounds:`.
            "no_verdict": True,
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
            "no_verdict": True,
        }
    # The fallback RAN but may not have judged anything: an unreadable reply, or an
    # error envelope, is an availability failure exactly like Codex's. This used to
    # synthesise `{"verdict": "block"}` and record it as a counted round, so a mute
    # reviewer could satisfy `Adversarial rounds:` — the very thing this branch is
    # about, one site short.
    result = outcome(extract_json(out), out, "claude-fallback")
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
    ap.add_argument(
        "--record-round",
        action="store_true",
        help="read a reviewer payload on stdin and append it to this feature's "
        "review.jsonl, then exit. For callers that run a reviewer themselves "
        "(tools/codex_review.sh) so no round goes unrecorded.",
    )
    args = ap.parse_args()

    if args.record_round:
        # A round the caller already ran. Normalize it the same way review() would,
        # so a directly-invoked reviewer and a dispatched one produce the same ledger.
        raw = sys.stdin.read()
        payload = extract_json(raw)
        # An UNREADABLE round is not a round that did not happen (CLAUDE.md rule 3):
        # a real Codex usage-limit envelope carries `result: null`, an empty
        # `rawOutput`, and its failure text in `parseError` / `codex.stderr`, so
        # nothing verdict-bearing survives extract_json — precisely the failure the
        # ledger most needs, and the one this path exists to keep. It is recorded,
        # fails closed, and carries the reviewer's own words; it is just not COUNTED.
        #
        # One question, one answer: `outcome` decides round-vs-attempt here exactly
        # as it does on the dispatched path. It also catches the case this branch
        # kept missing — a PARSEABLE `{"verdict": "error"}` outage envelope, which
        # is_rate_limited already reads as "unavailable" but which this path used to
        # normalize into a counted BLOCK round.
        reviewer = (payload or {}).get("reviewer", "codex")
        result = outcome(payload, raw, reviewer)
        path = append_round(result)
        kind = "attempt (no verdict)" if result.get("no_verdict") else "round"
        print(
            f"(recorded {kind} -> {path})" if path else "(no ATP_FEATURE_ID; not recorded)",
            file=sys.stderr,
        )
        return 0

    if args.status:
        return cmd_status()
    if not CRITIC_PROMPT.is_file():
        print(json.dumps({"verdict": "error", "reason": f"missing {CRITIC_PROMPT}"}))
        return 2
    return emit(review(args.base_ref, force_claude=args.force_claude))


if __name__ == "__main__":
    raise SystemExit(main())
