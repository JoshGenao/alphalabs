"""L1 — SAFETY_PATH_RE coverage (tools/critic_check.py).

Heartbeat freshness is the INPUT to stale-data blocking. If the heartbeat is wrong,
the `stale[_-]?data` guard never fires and a strategy trades on data nobody is
checking — yet no heartbeat path matched SAFETY_PATH_RE while `kill_switch` did. So
SRS-MD-003 could retune the live heartbeat producer with no paired tests/domain/
test demanded. That session shipped one voluntarily and recorded the gap for the
operator; this closes it.

The first proposed fix was `/heartbeat\\.py`, which would have missed
crates/atp-market-data/src/bin/md003_heartbeat_cli.rs — the Rust producer the
session actually changed.
"""

from __future__ import annotations

import critic_check as c
import pytest

pytestmark = pytest.mark.unit


def _blocks(files):
    diff = c.DiffSlice(
        files_changed=tuple(files),
        files_added=(),
        files_deleted=(),
        unified_diff="",
        commit_message="feat: x",
    )
    report = c.Report()
    c.check_safety_critical_paired(diff, report)
    return [f for f in report.findings if f.severity == "block"]


@pytest.mark.parametrize(
    "path",
    [
        "python/atp_dashboard/heartbeat.py",
        "crates/atp-market-data/src/bin/md003_heartbeat_cli.rs",  # the .py-only fix missed this
        "crates/atp-market-data/tests/srs_md_003_heartbeat_freshness.rs",
        "tests/unit/test_heartbeat_source.py",
    ],
)
def test_heartbeat_paths_are_safety_paths(path):
    assert c.SAFETY_PATH_RE.search(path), f"{path} must be a safety path"


def test_kill_switch_coverage_is_not_regressed():
    """The token that already worked must keep working."""
    assert c.SAFETY_PATH_RE.search("crates/atp-execution/src/kill_switch.rs")


def test_a_heartbeat_change_without_a_domain_test_is_blocked():
    blocks = _blocks(["python/atp_dashboard/heartbeat.py"])
    assert blocks and blocks[0].rule == "safety:paired-test-required"


def test_a_heartbeat_change_with_a_domain_test_passes():
    assert (
        _blocks(
            [
                "python/atp_dashboard/heartbeat.py",
                "tests/domain/test_heartbeat_staleness.py",
            ]
        )
        == []
    )


def test_unrelated_paths_are_still_unaffected():
    assert _blocks(["python/atp_strategy/indicators.py"]) == []


def test_documentation_carve_outs_still_apply():
    """A note or playbook NAMED for a safety subject carries no behavior to test."""
    assert _blocks(["docs/playbooks/safety-paths.md"]) == []
    assert _blocks(["progress.d/session-SRS-SAFE-001.md"]) == []


@pytest.mark.parametrize(
    "path",
    [
        "crates/atp-orchestrator/src/live_host/mod.rs",
        "crates/atp-orchestrator/src/live_host/server.rs",
        "crates/atp-orchestrator/src/bin/live_execution_host.rs",
    ],
)
def test_live_execution_host_paths_are_safety_paths(path):
    """SRS-EXE-001: the process that routes real orders to IB is a safety path."""
    assert c.SAFETY_PATH_RE.search(path), f"{path} must be a safety path"


def test_feature_evidence_is_exempt_but_same_token_source_is_not():
    """SRS-EXE-001's own id is a safety token, so its evidence path matched by name alone."""
    assert _blocks([".harness/runs/SRS-EXE-001/review.jsonl"]) == []
    # The same token on a source path (no "test" in it) still demands a domain test.
    blocks = _blocks(["crates/atp-execution/src/srs_exe_001_example.rs"])
    assert blocks and blocks[0].rule == "safety:paired-test-required"


# --------------------------------------------------------------------------- #
# Comment-only changes to a Rust safety file need no paired domain test: the
# comment-stripped source is identical, so the program is unchanged. Both
# directions are pinned: what must be exempt, and what must still block.
# --------------------------------------------------------------------------- #

SAFETY_RS = "crates/atp-execution/src/order_routing.rs"

BEFORE = """/// Route an order. See [`ExecutionEngine`].
pub fn route(x: u32) -> &'static str {
    // a line comment
    let url = "http://example.com/a//b";  /* block /* nested */ still */
    let c = '/';
    let r = r#"raw // not a comment "# ;
    if x > 1 { "big" } else { "small" }
}
"""


@pytest.mark.parametrize(
    "after",
    [
        BEFORE.replace("/// Route an order. See [`ExecutionEngine`].", "/// Route an order."),
        BEFORE.replace("/// Route", "// Route"),
        BEFORE.replace("// a line comment", "// a line comment\n    // and another"),
        BEFORE.replace("/* block /* nested */ still */", "/* rewritten */"),
        BEFORE.replace("    if x", "\n\n    if x"),
    ],
    ids=["doc-text", "doc-to-plain", "added-comment-line", "nested-block", "blank-lines"],
)
def test_comment_and_layout_changes_strip_to_the_same_program(after):
    assert c.rust_code_without_comments(BEFORE) == c.rust_code_without_comments(after)


@pytest.mark.parametrize(
    "after",
    [
        BEFORE.replace("a//b", "a/b"),  # a "//" inside a string is code
        BEFORE.replace("raw // not a comment", "raw / not a comment"),  # inside a raw string
        BEFORE.replace("'/'", "'*'"),  # a char literal
        BEFORE.replace("x > 1", "x > 2"),
        BEFORE.replace("// a line comment", "let y = 1; // a line comment"),
        BEFORE.replace('"small"', '"small "'),  # whitespace inside a string
    ],
    ids=[
        "slashes-in-string",
        "raw-string",
        "char-literal",
        "code",
        "code-before-comment",
        "string-space",
    ],
)
def test_changes_inside_literals_or_code_are_not_comment_only(after):
    assert c.rust_code_without_comments(BEFORE) != c.rust_code_without_comments(after)


def test_lifetimes_and_escaped_chars_do_not_start_a_literal():
    src = "fn f<'a>(s: &'a str) -> char { let q = '\\''; let b = '\\\\'; s.chars().next().unwrap() } // tail"
    stripped = c.rust_code_without_comments(src)
    assert stripped.endswith("unwrap() }"), stripped
    assert "'\\''" in stripped and "'\\\\'" in stripped


def _paired_blocks(monkeypatch, versions, *, added=(), deleted=(), refs=("A", "B")):
    monkeypatch.setattr(c, "_file_at", lambda ref, path: versions.get((ref, path)))
    diff = c.DiffSlice(
        files_changed=(SAFETY_RS,),
        files_added=tuple(added),
        files_deleted=tuple(deleted),
        unified_diff="",
        commit_message="docs: x",
        old_ref=refs[0],
        new_ref=refs[1],
    )
    report = c.Report()
    c.check_safety_critical_paired(diff, report)
    return [f for f in report.findings if f.severity == "block"]


def test_a_comment_only_safety_change_needs_no_domain_test(monkeypatch):
    after = BEFORE.replace("/// Route an order. See [`ExecutionEngine`].", "// Route an order.")
    assert _paired_blocks(monkeypatch, {("A", SAFETY_RS): BEFORE, ("B", SAFETY_RS): after}) == []


def test_a_code_change_to_a_safety_file_is_still_blocked(monkeypatch):
    after = BEFORE.replace("x > 1", "x >= 1")
    blocks = _paired_blocks(monkeypatch, {("A", SAFETY_RS): BEFORE, ("B", SAFETY_RS): after})
    assert len(blocks) == 1 and SAFETY_RS in blocks[0].message


def test_the_carve_out_fails_closed(monkeypatch):
    same = {("A", SAFETY_RS): BEFORE, ("B", SAFETY_RS): BEFORE}
    # an added or deleted file is never comment-only
    assert _paired_blocks(monkeypatch, same, added=(SAFETY_RS,))
    assert _paired_blocks(monkeypatch, same, deleted=(SAFETY_RS,))
    # an unreadable side, or an unknown ref, is never comment-only
    assert _paired_blocks(monkeypatch, {("A", SAFETY_RS): BEFORE})
    assert _paired_blocks(monkeypatch, same, refs=(None, None))
