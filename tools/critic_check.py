#!/usr/bin/env python3
"""Critic Agent — deterministic layer.

Mechanical pre-commit checks that don't need an LLM. Pairs with
``prompts/critic_prompt.md`` for the judgment-heavy second pass.

Exit codes:
    0 — approve or warn-only
    1 — at least one BLOCK finding (commit must not proceed)
    2 — internal/usage error

Output: ``--format json`` (machine) or ``--format text`` (human).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]

Severity = Literal["block", "warn", "info"]
Verdict = Literal["block", "warn", "approve"]


# ----------------------------------------------------------------------------
# Diff helpers
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class DiffSlice:
    files_changed: tuple[str, ...]
    files_added: tuple[str, ...]
    files_deleted: tuple[str, ...]
    unified_diff: str
    commit_message: str = ""
    # Where the two sides of the diff can be read, for checks that need whole files
    # (the comment-only carve-out). `old_ref` is a commit; `new_ref` is a commit,
    # INDEX (staged content) or WORKTREE. None = unknown, and such checks fail closed.
    old_ref: str | None = None
    new_ref: str | None = None


def _run_git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


INDEX = "INDEX"
WORKTREE = "WORKTREE"


def staged_slice() -> DiffSlice:
    sl = _slice_from(["--cached"])
    return DiffSlice(
        files_changed=sl.files_changed,
        files_added=sl.files_added,
        files_deleted=sl.files_deleted,
        unified_diff=sl.unified_diff,
        old_ref="HEAD",
        new_ref=INDEX,
    )


def _range_refs(commit_range: str) -> tuple[str, str]:
    """The two sides `git diff <commit_range>` compares."""
    if "..." in commit_range:
        left, right = commit_range.split("...", 1)
        right = right or "HEAD"
        return _run_git(["merge-base", left or "HEAD", right]).strip(), right
    if ".." in commit_range:
        left, right = commit_range.split("..", 1)
        return left or "HEAD", right or "HEAD"
    return commit_range, WORKTREE


def range_slice(commit_range: str) -> DiffSlice:
    sl = _slice_from([commit_range])
    msg = _run_git(["log", "-1", "--format=%B", commit_range.split("..")[-1] or "HEAD"])
    old_ref, new_ref = _range_refs(commit_range)
    return DiffSlice(
        files_changed=sl.files_changed,
        files_added=sl.files_added,
        files_deleted=sl.files_deleted,
        unified_diff=sl.unified_diff,
        commit_message=msg.strip(),
        old_ref=old_ref,
        new_ref=new_ref,
    )


def _file_at(ref: str | None, path: str) -> str | None:
    """`path` as it is at `ref` (a commit, INDEX or WORKTREE); None if unreadable."""
    if ref is None:
        return None
    if ref == WORKTREE:
        try:
            return (ROOT / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    spec = f":{path}" if ref == INDEX else f"{ref}:{path}"
    try:
        return _run_git(["show", spec])
    except RuntimeError:
        return None


def rust_code_without_comments(src: str) -> str:
    """Rust source with every comment removed and whitespace outside literals collapsed.

    Two versions of a file with equal results differ only in comments (doc comments
    included) and layout, so they compile to the same program. String, raw-string,
    byte-string and char literals are copied verbatim, so a `//` or `/*` inside one is
    never mistaken for a comment; block comments nest, as in Rust.
    """
    out: list[str] = []
    i, n = 0, len(src)

    def ident(ch: str) -> bool:
        return ch.isalnum() or ch == "_"

    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            depth, i = 1, i + 2
            while i < n and depth:
                if src.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif src.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if out and out[-1] != " ":
                out.append(" ")
            continue
        raw = re.match(r'(?:b|c)?r(#*)"', src[i:]) if ch in "brc" else None
        if raw and (i == 0 or not ident(src[i - 1])):
            close = '"' + raw.group(1)
            end = src.find(close, i + raw.end())
            end = n if end < 0 else end + len(close)
            out.append(src[i:end])
            i = end
            continue
        if ch == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            out.append(src[i : j + 1])
            i = j + 1
            continue
        if ch == "'":
            if nxt == "\\":
                end = src.find("'", i + 3 if src[i + 2 : i + 3] == "'" else i + 2)
                end = n if end < 0 else end + 1
                out.append(src[i:end])
                i = end
                continue
            if i + 2 < n and src[i + 2] == "'":
                out.append(src[i : i + 3])
                i += 3
                continue
            out.append(ch)  # a lifetime or label
            i += 1
            continue
        if ch.isspace():
            if out and out[-1] != " ":
                out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out).strip()


def comment_only_change(diff: DiffSlice, path: str) -> bool:
    """True only when `path` is a Rust file present on both sides of the diff and
    its comment-stripped source is identical. Anything else (added, deleted, not
    Rust, unreadable, or a ref that is not known) is NOT comment-only: fail closed."""
    if not path.endswith(".rs") or path in diff.files_added or path in diff.files_deleted:
        return False
    old = _file_at(diff.old_ref, path)
    new = _file_at(diff.new_ref, path)
    if old is None or new is None:
        return False
    return rust_code_without_comments(old) == rust_code_without_comments(new)


def _slice_from(diff_args: list[str]) -> DiffSlice:
    name_status = _run_git(["diff", "--name-status", *diff_args]).splitlines()
    changed: list[str] = []
    added: list[str] = []
    deleted: list[str] = []
    for line in name_status:
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        path = parts[-1]
        changed.append(path)
        if status.startswith("A"):
            added.append(path)
        elif status.startswith("D"):
            deleted.append(path)
    unified = _run_git(["diff", "-U0", *diff_args])
    return DiffSlice(
        files_changed=tuple(changed),
        files_added=tuple(added),
        files_deleted=tuple(deleted),
        unified_diff=unified,
    )


# ----------------------------------------------------------------------------
# Findings
# ----------------------------------------------------------------------------


@dataclass
class Finding:
    severity: Severity
    rule: str
    message: str
    file: str | None = None
    line: int | None = None

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "rule": self.rule,
            "message": self.message,
            "file": self.file,
            "line": self.line,
        }


@dataclass
class Report:
    verdict: Verdict = "approve"
    findings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)
        if finding.severity == "block":
            self.verdict = "block"
        elif finding.severity == "warn" and self.verdict != "block":
            self.verdict = "warn"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "findings": [f.to_dict() for f in self.findings],
        }


# ----------------------------------------------------------------------------
# Added-line iteration over a unified diff
# ----------------------------------------------------------------------------


def iter_added_lines(unified_diff: str) -> Iterable[tuple[str, int, str]]:
    """Yield (file, line_no, content) for each line ADDED by the diff."""
    current_file: str | None = None
    current_line = 0
    for raw in unified_diff.splitlines():
        if raw.startswith("+++ b/"):
            current_file = raw[6:]
            current_line = 0
            continue
        if raw.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", raw)
            if m:
                current_line = int(m.group(1))
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            if current_file is not None:
                yield current_file, current_line, raw[1:]
            current_line += 1
        elif not raw.startswith("-"):
            current_line += 1


# ----------------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------------


# Per-provider key patterns. Each must be unambiguous enough to avoid false
# positives on harmless variable names — anchor on a recognizable prefix.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws-secret", re.compile(r"\baws_secret_access_key\s*=\s*['\"][A-Za-z0-9/+=]{30,}")),
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("openai", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}\b")),
    ("github-token", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("databento", re.compile(r"\bdb-[A-Za-z0-9]{20,}\b")),
    ("private-key-pem", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "hardcoded-api-key",
        re.compile(r"""(?i)\b(?:api[_-]?key|secret|token)\s*=\s*['"][A-Za-z0-9_\-]{16,}['"]"""),
    ),
]

ENV_FILE_RE = re.compile(r"(^|/)\.env(\.[A-Za-z0-9_]+)?$")

SAFETY_PATH_RE = re.compile(
    r"(kill[_-]?switch|connectivity|stale[_-]?data|live[_-]?mode|safety"
    # Heartbeat freshness is the input to stale-data blocking: if the heartbeat is
    # wrong, `stale[_-]?data` above never fires and a strategy trades on data nobody
    # is checking. It matched NOTHING here while kill_switch did, so SRS-MD-003 could
    # retune the live heartbeat producer with no paired tests/domain/ test required —
    # that session shipped one voluntarily and left the gap recorded for the operator.
    # A bare token, not the narrower `/heartbeat\.py` first proposed: that missed
    # crates/atp-market-data/src/bin/md003_heartbeat_cli.rs, the Rust source this is
    # actually about. Covers all 8 heartbeat paths in the tree, 0 of which are under
    # the documentation carve-outs below.
    r"|heartbeat"
    r"|subscription[_-]?limit|market[_-]?data[_-]?line"
    r"|ingestion[_-]?validation|record[_-]?quarantine"
    r"|pacing[_-]?budget|ingestion[_-]?schedule"
    r"|orchestrator[_-]?lifecycle|strategy[_-]?container"
    r"|resource[_-]?profile"
    r"|workload[_-]?priority|host[_-]?memory[_-]?safety"
    r"|deployment[_-]?version|source[_-]?hash"
    r"|strategy[_-]?api[_-]?parity|paper[_-]?live[_-]?parity"
    r"|trading[_-]?calendar|scheduler[_-]?contract"
    r"|strategy[_-]?api[_-]?subscriptions|asset[_-]?class[_-]?guard"
    r"|strategy[_-]?api[_-]?order[_-]?events|order[_-]?event[_-]?payload"
    r"|callback[_-]?latency"
    r"|strategy[_-]?api[_-]?warmup|warm[_-]?up[_-]?replay"
    r"|warmup[_-]?controller|warmup[_-]?state|warmup[_-]?bars"
    r"|indicator[_-]?initialization|historical[_-]?replay"
    r"|strategy[_-]?api[_-]?indicators|indicator[_-]?parity"
    r"|pandas[_-]?ta|ta[_-]?lib|technical[_-]?indicators"
    r"|strategy[_-]?api[_-]?documentation|api[_-]?documentation"
    r"|strategy[_-]?example|example[_-]?strateg(y|ies)"
    r"|readiness[_-]?gate|startup[_-]?readiness|pre[_-]?trade"
    r"|operator[_-]?override|atp[_-]?readiness"
    r"|operator[_-]?workflow[_-]?surface|operator[_-]?workflow"
    r"|api[_-]?surface[_-]?coverage|ac[_-]?workflow"
    r"|operator[_-]?interface[_-]?runtime|atp[_-]?runtime"
    r"|handler[_-]?registry|deferred[_-]?handler|bind[_-]?policy"
    r"|log[_-]?record|atp[_-]?logging|log[_-]?sink"
    r"|log[_-]?dispatch|log[_-]?class|log[_-]?route"
    r"|subscription[_-]?fanout|subscription[_-]?fan[_-]?out"
    r"|consolidated[_-]?subscription|subscription[_-]?registry"
    r"|subscription[_-]?change|market[_-]?data[_-]?fan[_-]?out"
    r"|hot[_-]?swap|demotion|liquidation[_-]?timeout"
    r"|promotion[_-]?block|operator[_-]?alert"
    r"|paper[_-]?order|sim[_-]?order|order[_-]?intake|order[_-]?routing"
    r"|no[_-]?broker[_-]?route|internal[_-]?simulation[_-]?route"
    r"|fill[_-]?model|sim[_-]?fill|fill[_-]?decision|fill[_-]?trigger"
    r"|market[_-]?snapshot|volume[_-]?cap"
    r"|virtual[_-]?ledger|position[_-]?ledger|virtual[_-]?position"
    r"|sim[_-]?ledger|average[_-]?cost|avg[_-]?cost"
    r"|realized[_-]?pnl|unrealized[_-]?pnl|mark[_-]?to[_-]?market"
    r"|paper[_-]?state|sim[_-]?state|state[_-]?persistence"
    r"|persist|snapshot|restore|checkpoint|paper[_-]?persist"
    r"|performance[_-]?metric|paper[_-]?metric|perf[_-]?metric|metric"
    r"|sharpe|sortino|drawdown|annualized|win[_-]?rate"
    r"|/benchmark\.rs|benchmark[_-]?check|benchmark[_-]?comparison"
    r"|benchmark[_-]?selection|benchmark[_-]?source|benchmark[_-]?contract"
    r"|srs[_-]?bt[_-]?005"
    r"|/backtest_store\.rs|backtest[_-]?store[_-]?check|backtest[_-]?store[_-]?contract"
    r"|backtest[_-]?record|srs[_-]?bt[_-]?009"
    r"|/factor_analysis\.rs|factor[_-]?analysis[_-]?check|factor[_-]?analysis[_-]?contract"
    r"|factor[_-]?tear[_-]?sheet|srs[_-]?bt[_-]?006"
    r"|/determinism\.rs|determinism[_-]?check|determinism[_-]?contract"
    r"|backtest[_-]?determinism|srs[_-]?bt[_-]?010"
    r"|/cost\.rs|backtest[_-]?cost[_-]?check|backtest[_-]?cost[_-]?contract"
    r"|bt002[_-]?cost[_-]?cli|srs[_-]?bt[_-]?002"
    r"|/sim\.rs|sim[_-]?cost[_-]?check|sim[_-]?cost[_-]?contract"
    r"|bt003[_-]?shared[_-]?cost[_-]?cli|shared[_-]?cost[_-]?family|srs[_-]?bt[_-]?003"
    r"|/virtual_ledger\.rs|sim003[_-]?ledger[_-]?cli|sim[_-]?ledger[_-]?check|srs[_-]?sim[_-]?003"
    r"|sim002[_-]?fill[_-]?cli|sim[_-]?fill[_-]?check|srs[_-]?sim[_-]?002"
    r"|/factor_job\.rs|factor[_-]?job[_-]?check|factor[_-]?job[_-]?contract"
    r"|srs[_-]?fac[_-]?001"
    r"|/designation\.rs|live[_-]?designation|srs[_-]?exe[_-]?001"
    # SRS-EXE-001: the live execution host is the production call site of route_order -
    # the one process whose orders reach IB. Neither its module (live_host/) nor its
    # binary (live_execution_host) matched any token above. 0 tracked-path collisions.
    r"|live[_-]?host|live[_-]?execution[_-]?host"
    r"|/order_lifecycle\.rs|order[_-]?lifecycle|srs[_-]?exe[_-]?008"
    r"|/order_event\.rs|order[_-]?event[_-]?dispatch|srs[_-]?sdk[_-]?004"
    r"|/order_type\.rs|order[_-]?type|srs[_-]?exe[_-]?003"
    r"|/order_routing\.rs|order[_-]?routing|srs[_-]?exe[_-]?002"
    r"|/interactive_brokers\.rs|ib[_-]?adapter|brokerage[_-]?runtime|srs[_-]?exe[_-]?006"
    r"|err001|error[_-]?envelope|error[_-]?handling[_-]?cli|structured[_-]?error"
    r"|srs[_-]?err[_-]?001"
    r"|/halt\.rs|paper[_-]?halt|halt[_-]?gate|haltable[_-]?paper"
    r"|paper[_-]?engine[_-]?state|sim[_-]?halt[_-]?check|srs[_-]?safe[_-]?001"
    r"|/coverage\.rs|coverage[_-]?manifest|coverage[_-]?gate|coverage[_-]?frontier"
    r"|corporate[_-]?action[_-]?coverage|query[_-]?split[_-]?adjusted"
    r"|data011|split[_-]?adjusted[_-]?serving|srs[_-]?data[_-]?011)",
    re.IGNORECASE,
)

# Vendor SDK tokens that must not appear in core (non-adapter) Rust crates or
# in the Python strategy boundary.
VENDOR_TOKENS = (
    "interactive_brokers",
    "ib_insync",
    "ibapi",
    "databento",
    "sharadar",
)
CORE_PATH_PREFIXES = (
    "crates/atp-data/",
    "crates/atp-execution/",
    "crates/atp-factor-pipeline/",
    "crates/atp-strategy-engine/",
    "crates/atp-orchestrator/",
    "crates/atp-simulation/",
    "crates/atp-types/",
    "python/atp_strategy/",
)

PRICE_FIELD_RE = re.compile(
    r"\b(price|notional|fill_price|limit_price|stop_price|avg_price)\b\s*[+\-*/]"
)

SKIP_DECORATOR_RE = re.compile(r"@(?:pytest\.mark\.skip|unittest\.skip)\b(?!.*reason\s*=)")

SRS_REF_RE = re.compile(r"\bSRS-[A-Z]+-\d+\b")


def check_secrets(diff: DiffSlice, report: Report) -> None:
    for path, line_no, content in iter_added_lines(diff.unified_diff):
        for rule, pattern in SECRET_PATTERNS:
            if pattern.search(content):
                report.add(
                    Finding(
                        severity="block",
                        rule=f"secret:{rule}",
                        message=f"possible secret committed (rule={rule})",
                        file=path,
                        line=line_no,
                    )
                )
    for path in diff.files_added:
        if ENV_FILE_RE.search(path):
            report.add(
                Finding(
                    severity="block",
                    rule="secret:env-file",
                    message=f"committing env file is forbidden: {path}",
                    file=path,
                )
            )


def check_test_deletion(diff: DiffSlice, report: Report) -> None:
    for path in diff.files_deleted:
        if path.startswith("tests/") and path.endswith(".py"):
            report.add(
                Finding(
                    severity="block",
                    rule="tests:deletion",
                    message=f"test file deleted: {path}",
                    file=path,
                )
            )
    for path, line_no, content in iter_added_lines(diff.unified_diff):
        if SKIP_DECORATOR_RE.search(content):
            report.add(
                Finding(
                    severity="block",
                    rule="tests:skip-without-reason",
                    message="@pytest.mark.skip / @unittest.skip without reason= is forbidden",
                    file=path,
                    line=line_no,
                )
            )


def check_safety_critical_paired(diff: DiffSlice, report: Report) -> None:
    # Session notes under progress.d/ are documentation, not safety code: a note NAMED after a
    # safety feature (session-SRS-DATA-011.md, session-SRS-SAFE-001.md, ...) matches SAFETY_PATH_RE
    # by its feature id alone, so without this carve-out the integrator's own fold-and-flip commit
    # (which DELETES the note) and any notes-only handoff edit would demand a tests/domain diff that
    # has no behavior to test. The pairing requirement stays intact for every code path.
    #
    # docs/playbooks/ is the same shape: the lesson files are named for the subject they cover
    # (safety-paths.md, and any future connectivity-/live-mode- playbook), so they match by
    # filename while carrying no behavior at all. Both carve-outs are DOCUMENTATION prefixes --
    # no source, test, or config path can reach them.
    #
    # .harness/runs/ is the same shape again: per-feature evidence (review rounds, step
    # output, EVIDENCE.md) named for the feature, so a feature whose id is itself a safety
    # token (srs-exe-001) matched by its evidence path alone, while integrate refuses to run
    # unless that evidence is committed. Evidence carries no behavior. (SRS-EXE-001, 2026-10-10)
    #
    # A Rust safety file whose change is COMMENTS ONLY is exempt too: its comment-stripped
    # source is identical on both sides, so the compiled program is unchanged and there is
    # no behaviour for a domain test to pin. The comparison uses a string-aware lexer, so a
    # change inside a string literal that merely looks like a comment still counts as code.
    # (SRS-MD-004 follow-up: clearing 410 rustdoc warnings touched 23 safety files.)
    _DOC_ONLY_PREFIXES = ("progress.d/", "docs/playbooks/", ".harness/runs/")
    safety_files = [
        p
        for p in diff.files_changed
        if SAFETY_PATH_RE.search(p)
        and "test" not in p
        and not p.startswith(_DOC_ONLY_PREFIXES)
        and not comment_only_change(diff, p)
    ]
    if not safety_files:
        return
    has_domain_test_diff = any(
        p.startswith("tests/domain/") and p.endswith(".py") for p in diff.files_changed
    )
    if not has_domain_test_diff:
        report.add(
            Finding(
                severity="block",
                rule="safety:paired-test-required",
                message=(
                    "safety-critical files changed without a paired tests/domain/ "
                    f"diff: {', '.join(safety_files)}"
                ),
            )
        )


def check_vendor_leakage(diff: DiffSlice, report: Report) -> None:
    for path, line_no, content in iter_added_lines(diff.unified_diff):
        if not any(path.startswith(prefix) for prefix in CORE_PATH_PREFIXES):
            continue
        for token in VENDOR_TOKENS:
            if re.search(rf"\b{re.escape(token)}\b", content):
                report.add(
                    Finding(
                        severity="block",
                        rule="adapter-isolation:vendor-import",
                        message=f"vendor token {token!r} found in core path {path}",
                        file=path,
                        line=line_no,
                    )
                )
                break


def check_float_on_price(diff: DiffSlice, report: Report) -> None:
    for path, line_no, content in iter_added_lines(diff.unified_diff):
        if not (path.endswith(".py") or path.endswith(".rs")):
            continue
        if PRICE_FIELD_RE.search(content):
            report.add(
                Finding(
                    severity="warn",
                    rule="money:float-arithmetic",
                    message=(
                        "arithmetic on a price-named field — verify rounding/tolerance "
                        "or migrate to Decimal"
                    ),
                    file=path,
                    line=line_no,
                )
            )


def check_srs_refs(diff: DiffSlice, report: Report) -> None:
    if not diff.commit_message:
        return  # only meaningful in --range mode
    public_api_touched = any(
        p.endswith(".py")
        and (
            p.startswith("python/atp_api/")
            or p.startswith("python/atp_ws/")
            or p.startswith("python/atp_cli/")
            or p.startswith("python/atp_strategy/")
        )
        for p in diff.files_changed
    )
    if public_api_touched and not SRS_REF_RE.search(diff.commit_message):
        report.add(
            Finding(
                severity="warn",
                rule="traceability:missing-srs-ref",
                message=(
                    "public-API change without an SRS-XXX-NNN reference in the commit message"
                ),
            )
        )


CHECKS = (
    check_secrets,
    check_test_deletion,
    check_safety_critical_paired,
    check_vendor_leakage,
    check_float_on_price,
    check_srs_refs,
)


# ----------------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------------


def run(diff: DiffSlice) -> Report:
    report = Report()
    for check in CHECKS:
        check(diff, report)
    return report


def render_text(report: Report) -> str:
    if not report.findings:
        return "critic: APPROVE — no findings"
    lines = [f"critic: {report.verdict.upper()} — {len(report.findings)} finding(s)"]
    for f in report.findings:
        loc = ""
        if f.file:
            loc = f" {f.file}"
            if f.line:
                loc += f":{f.line}"
        lines.append(f"  [{f.severity}] {f.rule}{loc} — {f.message}")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group()
    src.add_argument(
        "--staged",
        action="store_true",
        help="Review staged (cached) changes — default if no source flag is given.",
    )
    src.add_argument(
        "--range",
        dest="commit_range",
        metavar="A..B",
        help="Review the diff between commits A..B.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.commit_range:
            diff = range_slice(args.commit_range)
        else:
            diff = staged_slice()
    except RuntimeError as error:
        print(f"critic: ERROR — {error}", file=sys.stderr)
        return 2

    report = run(diff)

    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_text(report))

    return 1 if report.verdict == "block" else 0


if __name__ == "__main__":
    raise SystemExit(main())
