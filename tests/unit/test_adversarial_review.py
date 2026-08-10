"""L1 — Unit tests for the Codex→Claude adversarial-review dispatcher.

Covers the pure decision logic that decides *which* reviewer runs and how a
verdict is normalized — the part that must be correct for a Codex usage limit to
transparently fail over to a fresh-context Claude review:
  - is_rate_limited: detect the runtime limit the old trigger missed
  - parse_reset_time: "try again at H:MM AM/PM" → absolute local datetime
  - normalize_verdict: any reviewer → canonical block|warn|approve
  - extract_json: pull the verdict object out of a prose-wrapped reply
  - cooldown prediction from the cache and the plugin job state
"""

import json
import subprocess
from datetime import datetime, timedelta

import adversarial_review as ar
import pytest

pytestmark = pytest.mark.unit


# --- is_rate_limited --------------------------------------------------------
def test_is_rate_limited_detects_usage_limit_prose():
    out = "You've hit your usage limit. ... or try again at 1:35 PM."
    assert ar.is_rate_limited(out, 1) is True


def test_is_rate_limited_detects_error_verdict_payload():
    assert ar.is_rate_limited('{"verdict":"error","reason":"x"}', 0) is True


def test_is_rate_limited_false_on_clean_approve():
    assert ar.is_rate_limited('{"verdict":"approve","summary":"ok"}', 0) is False


# --- parse_reset_time -------------------------------------------------------
def test_parse_reset_time_pm():
    hit = datetime.fromisoformat("2026-07-02T13:00:00+00:00").astimezone()
    reset = ar.parse_reset_time("try again at 1:35 PM", hit)
    assert reset is not None and (reset.hour, reset.minute) == (13, 35)


def test_parse_reset_time_rolls_to_next_day_when_before_hit():
    # Limit hit at 11:00 PM local, reset "12:30 AM" → next calendar day.
    hit = datetime.now().astimezone().replace(hour=23, minute=0, second=0, microsecond=0)
    reset = ar.parse_reset_time("try again at 12:30 AM", hit)
    assert reset > hit and (reset.hour, reset.minute) == (0, 30)


def test_parse_reset_time_none_without_hint():
    assert ar.parse_reset_time("some unrelated error", datetime.now().astimezone()) is None


# --- normalize_verdict ------------------------------------------------------
def test_normalize_passthrough_canonical():
    for v in ("block", "warn", "approve"):
        assert ar.normalize_verdict({"verdict": v}, "codex")["verdict"] == v


def test_normalize_needs_attention_high_becomes_block():
    got = ar.normalize_verdict(
        {"verdict": "needs-attention", "findings": [{"severity": "high"}]}, "codex"
    )
    assert got["verdict"] == "block" and got["reviewer"] == "codex"


def test_normalize_needs_attention_low_becomes_warn():
    got = ar.normalize_verdict(
        {"verdict": "needs-attention", "findings": [{"severity": "low"}]}, "codex"
    )
    assert got["verdict"] == "warn"


def test_normalize_unknown_fails_closed_to_block():
    assert ar.normalize_verdict({"verdict": "???"}, "claude-fallback")["verdict"] == "block"


# --- extract_json -----------------------------------------------------------
def test_extract_json_fenced_and_bare_and_none():
    assert ar.extract_json('x\n```json\n{"verdict":"warn"}\n```\ny')["verdict"] == "warn"
    assert ar.extract_json('prefix {"verdict":"approve"} suffix')["verdict"] == "approve"
    assert ar.extract_json("no json at all") is None


def test_extract_json_unwraps_codex_json_envelope():
    # The codex companion's `--json` mode nests the verdict under `result`
    # (and duplicates it as a string in `rawOutput`); the top level has no
    # `verdict` key. Without unwrapping, this looks unparseable and Codex's
    # verdict is silently dropped for the Claude fallback.
    envelope = json.dumps(
        {
            "review": "Adversarial Review",
            "context": {"branch": "main"},
            "codex": {"status": 0, "stdout": '{"verdict":"needs-attention"}'},
            "result": {
                "verdict": "needs-attention",
                "summary": "target not identifiable",
                "findings": [{"severity": "critical", "title": "x"}],
            },
            "rawOutput": '{"verdict":"needs-attention"}',
            "parseError": None,
        }
    )
    got = ar.extract_json(envelope)
    assert got is not None, "codex --json envelope must be unwrapped, not dropped"
    assert got["verdict"] == "needs-attention"
    assert got["findings"][0]["severity"] == "critical"


def test_extract_json_falls_back_to_rawoutput_when_result_absent():
    envelope = json.dumps({"codex": {"status": 0}, "rawOutput": '{"verdict":"approve"}'})
    assert ar.extract_json(envelope)["verdict"] == "approve"


# --- cooldown prediction ----------------------------------------------------
def test_cooldown_from_cache_future_and_past(tmp_path, monkeypatch):
    now = datetime.now().astimezone()
    cache = tmp_path / ".codex_cooldown.json"
    monkeypatch.setattr(ar, "COOLDOWN_FILE", cache)

    cache.write_text(json.dumps({"until": (now + timedelta(hours=1)).isoformat()}))
    assert ar.cooldown_from_cache(now) is not None  # still cooling down

    cache.write_text(json.dumps({"until": (now - timedelta(hours=1)).isoformat()}))
    assert ar.cooldown_from_cache(now) is None  # window elapsed


def _state_dir_with_job(tmp_path, status, summary, completed="2026-07-02T13:00:00Z"):
    d = tmp_path / "alphalabs-wt-SRS-X-000-abc"
    d.mkdir(parents=True)
    (d / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "kind": "adversarial-review",
                        "status": status,
                        "summary": summary,
                        "updatedAt": completed,
                        "completedAt": completed,
                    }
                ],
            }
        )
    )
    return tmp_path


def test_cooldown_from_plugin_state_limited(tmp_path):
    summary = "You've hit your usage limit. try again at 11:59 PM."
    state_dir = _state_dir_with_job(tmp_path, "failed", summary)
    hit = datetime.fromisoformat("2026-07-02T13:00:00+00:00").astimezone()
    reset = ar.parse_reset_time(summary, hit)
    # Just before reset → still limited (returns the reset instant).
    got = ar.cooldown_from_plugin_state(state_dir, now=reset - timedelta(hours=1))
    assert got == reset
    # After reset → available again.
    assert ar.cooldown_from_plugin_state(state_dir, now=reset + timedelta(hours=1)) is None


def test_cooldown_from_plugin_state_completed_job_is_available(tmp_path):
    state_dir = _state_dir_with_job(tmp_path, "completed", "review done")
    assert ar.cooldown_from_plugin_state(state_dir, now=datetime.now().astimezone()) is None


# --- review telemetry (P1-1) -------------------------------------------------
# `Adversarial rounds:` is the only falsifiable claim the playbook system makes —
# coding_prompt.md calls it "a measurement, not decoration" — and it appeared in 1 of
# 38 session notes, because it was prose an agent had to remember to type. The
# reviewer knows the number; these pin that it records it and never breaks the review
# it is measuring.
def _result(verdict="block", reviewer="codex", rules=(("a:b", "block"), ("c:d", "warn"))):
    return {
        "verdict": verdict,
        "reviewer": reviewer,
        "reviewer_note": "",
        "findings": [{"rule": r, "severity": s} for r, s in rules],
    }


def test_round_record_dedupes_rules_and_separates_blocking():
    rec = ar.round_record(_result(rules=(("a:b", "block"), ("c:d", "warn"), ("a:b", "block"))))
    assert rec["n_findings"] == 3  # findings are not deduped …
    assert rec["rules"] == ["a:b", "c:d"]  # … but rule ids are
    assert rec["blocking_rules"] == ["a:b"]
    assert rec["verdict"] == "block" and rec["reviewer"] == "codex"


def test_append_round_is_a_no_op_without_a_feature_id(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("ATP_FEATURE_ID", raising=False)
    assert ar.append_round(_result()) is None
    assert not (tmp_path / ".harness").exists()


def test_append_round_accumulates_one_line_per_round(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "REPO_ROOT", tmp_path)
    for _ in range(3):
        path = ar.append_round(_result(), fid="F-1")
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert len(lines) == 3
    assert path == tmp_path / ".harness" / "runs" / "F-1" / "review.jsonl"


def test_append_round_never_fails_the_review_it_measures(tmp_path, monkeypatch):
    """Telemetry that can break the gate teaches agents to stop running the gate."""
    monkeypatch.setattr(ar, "REPO_ROOT", tmp_path)
    # A FILE where the runs directory must go: mkdir raises, append_round must not.
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "runs").write_text("not a directory")
    assert ar.append_round(_result(), fid="F-1") is None


def test_emit_returns_the_verdict_exit_code_regardless_of_telemetry(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ar, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("ATP_FEATURE_ID", raising=False)
    assert ar.emit(_result(verdict="block")) == 1
    assert ar.emit(_result(verdict="approve")) == 0
    assert ar.emit(_result(verdict="warn")) == 0


# --- both reviewers' finding schemas ------------------------------------------
# A real SRS-MD-003 session ran 7 Codex rounds and every one recorded rules=['?']
# and blocking_rules=[]. Codex describes a finding with `title`/`severity: high`;
# the Claude fallback uses `rule`/`severity: block`. Only the second was understood,
# which silently gutted the promotion-candidate mining the rule ids exist for.
def test_codex_findings_yield_a_rule_key():
    f = {"title": "Re-execution corrupts argv grouping", "severity": "high", "body": "..."}
    assert ar.finding_rule(f) == "re-execution corrupts argv grouping"
    assert ar.is_blocking(f) is True


def test_claude_findings_still_yield_their_rule_id():
    f = {"rule": "meta:critic-self-modification", "severity": "block"}
    assert ar.finding_rule(f) == "meta:critic-self-modification"
    assert ar.is_blocking(f) is True


@pytest.mark.parametrize(
    "sev,blocking",
    [
        ("block", True),
        ("critical", True),
        ("high", True),
        ("warn", False),
        ("medium", False),
        ("low", False),
        ("info", False),
        ("", False),
    ],
)
def test_both_severity_vocabularies_are_understood(sev, blocking):
    assert ar.is_blocking({"rule": "x", "severity": sev}) is blocking


def test_a_finding_with_no_identifying_key_is_not_silently_dropped():
    assert ar.finding_rule({"severity": "high", "body": "no title, no rule"}) == "?"


def test_round_record_of_a_real_codex_round_is_not_all_question_marks():
    """The exact shape that produced 7 useless ledger entries."""
    rec = ar.round_record(
        {
            "verdict": "block",
            "reviewer": "codex",
            "findings": [
                {"title": "Missing feature/SRS trace", "severity": "high"},
                {"title": "Re-execution corrupts argv grouping", "severity": "high"},
            ],
        }
    )
    assert rec["rules"] == ["missing feature/srs trace", "re-execution corrupts argv grouping"]
    assert len(rec["blocking_rules"]) == 2
    assert "?" not in rec["rules"]


def test_a_long_title_is_clipped_so_the_ledger_stays_groupable():
    long_title = "x" * 200
    assert len(ar.finding_rule({"title": long_title})) == 80


# --- an unreadable round is still a round -------------------------------------
# Three consecutive rounds of this branch failed to record the failing case, each
# time because the real shape differed from the one imagined. The last of them: a
# Codex usage-limit envelope carries `result: null`, an empty `rawOutput`, and its
# cause in parseError/codex.stderr — nothing verdict-bearing survives extract_json.
# CLAUDE.md rule 3: unreadable is not empty.
REAL_LIMIT_ENVELOPE = json.dumps(
    {
        "review": "adversarial-review",
        "result": None,
        "rawOutput": "",
        "parseError": "no JSON found in reply",
        "codex": {"stderr": "you've hit your usage limit, try again at 3:00 PM", "stdout": ""},
    }
)


def test_the_real_usage_limit_envelope_yields_no_verdict():
    """Pins WHY the round was dropped: extract_json genuinely cannot read it."""
    assert ar.extract_json(REAL_LIMIT_ENVELOPE) is None


def test_unreadable_reason_prefers_the_reviewers_own_words():
    assert "no JSON found in reply" in ar.unreadable_reason(REAL_LIMIT_ENVELOPE)


def test_unreadable_reason_falls_back_through_the_envelope():
    assert "usage limit" in ar.unreadable_reason(
        json.dumps({"codex": {"stderr": "you've hit your usage limit"}})
    )
    assert "boom" in ar.unreadable_reason(json.dumps({"reason": "boom"}))
    assert "(empty)" in ar.unreadable_reason("")


def test_an_unreadable_round_fails_closed_to_block():
    """Not 'approve', not 'warn', and above all not 'absent'."""
    result = ar.normalize_verdict(
        {"verdict": "block", "summary": ar.unreadable_reason(REAL_LIMIT_ENVELOPE), "findings": []},
        "codex",
    )
    rec = ar.round_record(result)
    assert rec["verdict"] == "block"
    assert rec["n_findings"] == 0
    assert "no JSON found in reply" in rec["summary"]


# ---------------------------------------------------------------------------
# Attempts vs rounds — the failover must leave a trace without inflating the count
# ---------------------------------------------------------------------------
def test_a_rate_limited_codex_is_recorded_on_the_dispatched_path(tmp_path, monkeypatch):
    """The defect four consecutive fixes walked past.

    run_codex sets ATP_REVIEW_DISPATCHED=1 so codex_review.sh does NOT record (else
    the round lands twice). review() then fell back to Claude and emitted only the
    FALLBACK — so on the one path the docs tell agents to use, a rate-limited Codex
    left no trace at all. The earlier fixes all hardened the *shell* path, which was
    never the one that dropped it.
    """
    monkeypatch.setattr(ar, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("ATP_FEATURE_ID", "F-DISPATCH")
    monkeypatch.setattr(ar, "codex_cooldown_until", lambda: None)
    monkeypatch.setattr(ar, "record_cooldown", lambda _out: None)
    monkeypatch.setattr(
        ar, "run_codex", lambda _b: (1, "you've hit your usage limit, try again at 3:00 PM")
    )
    monkeypatch.setattr(
        ar, "run_claude_fallback", lambda _b, timeout=900: (0, json.dumps({"verdict": "approve"}))
    )

    result = ar.review("origin/main")
    assert result["reviewer"] == "claude-fallback"
    ar.append_round(result)  # emit() does this; call it directly to keep the test pure

    recs = ar.read_records(tmp_path / ".harness" / "runs" / "F-DISPATCH" / "review.jsonl")
    kinds = [r["kind"] for r in recs]
    assert kinds == ["attempt", "round"], "the failed Codex attempt must be on the record"
    assert recs[0]["reviewer"] == "codex"
    assert "usage limit" in recs[0]["summary"], "carry the reviewer's own reason"
    assert recs[0]["verdict"] == "none", "an attempt has no opinion about the diff"


def test_an_unparseable_codex_reply_is_also_recorded_before_the_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("ATP_FEATURE_ID", "F-UNPARSE")
    monkeypatch.setattr(ar, "codex_cooldown_until", lambda: None)
    monkeypatch.setattr(ar, "run_codex", lambda _b: (0, "I could not complete the review."))
    monkeypatch.setattr(
        ar, "run_claude_fallback", lambda _b, timeout=900: (0, json.dumps({"verdict": "approve"}))
    )

    ar.review("origin/main")
    recs = ar.read_records(tmp_path / ".harness" / "runs" / "F-UNPARSE" / "review.jsonl")
    assert [r["kind"] for r in recs] == ["attempt"]
    assert ar.count_rounds(tmp_path / ".harness" / "runs" / "F-UNPARSE" / "review.jsonl") == 0


def test_a_successful_codex_review_records_exactly_one_round_and_no_attempt(tmp_path, monkeypatch):
    """The non-regression half: a healthy dispatch must not gain a spurious attempt."""
    monkeypatch.setattr(ar, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ar, "codex_cooldown_until", lambda: None)
    monkeypatch.setattr(ar, "run_codex", lambda _b: (0, json.dumps({"verdict": "approve"})))
    result = ar.review("origin/main")
    assert result["reviewer"] == "codex"
    path = ar.append_round(result, fid="F-OK")
    assert [r["kind"] for r in ar.read_records(path)] == ["round"]
    assert ar.count_rounds(path) == 1


def test_a_timed_out_fallback_blocks_but_is_not_a_round(tmp_path, monkeypatch):
    """CLAUDE.md rule 7: a reviewer TIMEOUT is not a verdict — retry it.

    It must still BLOCK (exit 1 halts the agent), but counting it would let a
    reviewer outage satisfy `Adversarial rounds:` without anyone reading the diff.
    """
    monkeypatch.setattr(ar, "REPO_ROOT", tmp_path)

    def _timeout(_b, timeout=900):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

    monkeypatch.setattr(ar, "run_claude_fallback", _timeout)
    result = ar.review("origin/main", force_claude=True)
    assert result["verdict"] == "block", "still fails closed"
    path = ar.append_round(result, fid="F-TIMEOUT")
    assert [r["kind"] for r in ar.read_records(path)] == ["attempt"]
    assert ar.count_rounds(path) == 0


def test_records_written_before_kind_existed_still_count_as_rounds(tmp_path):
    """Absent `kind` means OLD, not attempt.

    Defaulting the other way would silently rewrite every feature already on main
    down to zero rounds and fire note_rounds_mismatch across the whole board.
    """
    path = tmp_path / "review.jsonl"
    path.write_text('{"verdict":"block"}\n{"verdict":"approve"}\n', encoding="utf-8")
    assert ar.count_rounds(path) == 2
    assert ar.is_round({"verdict": "block"}) is True


def test_unreadable_telemetry_is_unknown_not_zero(tmp_path):
    """CLAUDE.md rule 3 — a truncated write must not read as 'never reviewed'."""
    path = tmp_path / "review.jsonl"
    path.write_text('{"verdict":"block"}\n{"verdict":  <-- truncated\n', encoding="utf-8")
    assert ar.read_records(path) is None
    assert ar.count_rounds(path) is None
    assert ar.count_rounds(tmp_path / "does-not-exist.jsonl") is None


# ---------------------------------------------------------------------------
# Malformed reviewer payloads must not kill the review they are measuring.
# Found by the judgment critic on this very branch: normalize_verdict filters with
# isinstance(f, dict) but finding_rule did not, so `findings: ["a string"]` passed
# normalization and then died with AttributeError INSIDE emit() — after the verdict
# had already printed. Pre-existing on main; the refactor did not fix it.
# ---------------------------------------------------------------------------
MALFORMED = [
    pytest.param(["a bare string"], 1, id="list_of_strings"),
    pytest.param([None], 1, id="list_of_none"),
    pytest.param([42], 1, id="list_of_ints"),
    pytest.param("totally wrong", 1, id="findings_is_a_string"),
    pytest.param(123, 1, id="findings_is_a_number"),
    pytest.param([{"rule": "ok", "severity": "high"}, "junk", None], 3, id="mixed"),
]


@pytest.mark.parametrize("findings,expected_n", MALFORMED)
def test_malformed_findings_never_crash_the_review(findings, expected_n, tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "REPO_ROOT", tmp_path)
    result = {"verdict": "block", "reviewer": "codex", "summary": "s", "findings": findings}
    path = ar.append_round(result, fid="F-MAL")  # must not raise
    rec = json.loads(path.read_text().splitlines()[-1])
    # Unreadable is UNKNOWN, never dropped and never inflated (CLAUDE.md rule 3):
    # a string is iterable, so len("totally wrong") once reported 13 findings.
    assert rec["n_findings"] == expected_n
    assert "?" in rec["rules"]


def test_emit_survives_a_malformed_payload_and_still_returns_the_verdict(tmp_path, monkeypatch):
    """The gate must outlive its own telemetry — emit() printed, then died."""
    monkeypatch.setattr(ar, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("ATP_FEATURE_ID", "F-EMIT")
    rc = ar.emit({"verdict": "block", "reviewer": "codex", "summary": "s", "findings": ["junk"]})
    assert rc == 1, "the verdict's exit code must survive a telemetry failure"


def test_a_well_formed_payload_is_unchanged(tmp_path, monkeypatch):
    """The non-regression half: normalizing must not blunt a real finding."""
    monkeypatch.setattr(ar, "REPO_ROOT", tmp_path)
    result = {
        "verdict": "block",
        "reviewer": "codex",
        "findings": [{"rule": "real-one", "severity": "high"}],
    }
    rec = json.loads(ar.append_round(result, fid="F-OK2").read_text().splitlines()[-1])
    assert rec["n_findings"] == 1
    assert rec["rules"] == ["real-one"] and rec["blocking_rules"] == ["real-one"]


def test_telemetry_can_never_raise_whatever_goes_wrong(tmp_path, monkeypatch):
    """The guard is broad on purpose: the ledger must not be able to kill the gate."""
    monkeypatch.setattr(ar, "REPO_ROOT", tmp_path)

    def _boom():
        raise RuntimeError("record building exploded")

    assert ar._append(_boom, fid="F-BOOM") is None  # returns None, does not raise


def test_findings_list_normalizes_the_container():
    assert ar.findings_list(None) == []
    assert ar.findings_list([1, 2]) == [1, 2]
    assert ar.findings_list("abc") == ["abc"], "a string is ONE unreadable finding, not three"
    assert ar.findings_list({"rule": "x"}) == [{"rule": "x"}]


# ---------------------------------------------------------------------------
# Round 6. The previous commit called itself a class fix and covered four of the
# SEVEN sites that decide round-vs-attempt; the reviewer found all three misses.
# `outcome()` is now the single place the question is asked — these pin every path
# to it, including the ones that must still count.
# ---------------------------------------------------------------------------
def test_a_parseable_error_envelope_is_an_outage_not_a_block_round():
    """is_rate_limited() already calls this "unavailable" — one shape, one meaning."""
    raw = '{"verdict":"error","reason":"you hit your usage limit"}'
    res = ar.outcome(ar.extract_json(raw), raw, "codex")
    assert res["no_verdict"] is True, "an error envelope is not a judgment of the diff"
    assert res["verdict"] == "block", "but it still halts the agent"
    assert ar.is_rate_limited(raw, 1), "the two must agree about the same envelope"


def test_a_fallback_that_ran_but_said_nothing_readable_is_an_attempt(monkeypatch):
    """It used to synthesise `{"verdict":"block"}` and record a COUNTED round, so a
    mute reviewer could satisfy `Adversarial rounds:` without reading anything."""
    monkeypatch.setattr(ar, "codex_cooldown_until", lambda: None)
    monkeypatch.setattr(
        ar, "run_claude_fallback", lambda _b, timeout=900: (0, "I cannot produce JSON.")
    )
    res = ar.review("origin/main", force_claude=True)
    assert res.get("no_verdict") is True
    assert res["verdict"] == "block"


def test_the_reader_cannot_kill_the_gate_either(tmp_path):
    """Guarding only the WRITE path left emit()'s count_rounds able to raise.

    A torn write mid-multibyte-character raises UnicodeDecodeError — a ValueError,
    which the old `except OSError` did not catch.
    """
    p = tmp_path / "review.jsonl"
    p.write_bytes(b'{"kind":"round","verdict":"block"}\n\xff\xfe not utf-8 \xff\n')
    assert ar.read_records(p) is None, "unknown, never a confident []"
    assert ar.count_rounds(p) is None


def test_emit_survives_an_unreadable_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("ATP_FEATURE_ID", "F-TORN")
    d = tmp_path / ".harness" / "runs" / "F-TORN"
    d.mkdir(parents=True)
    (d / "review.jsonl").write_bytes(b"\xff\xfe torn\n")
    rc = ar.emit({"verdict": "block", "reviewer": "codex", "summary": "s", "findings": []})
    assert rc == 1, "the verdict must outlive an unreadable ledger"


@pytest.mark.parametrize(
    "payload,expect_round",
    [
        pytest.param({"verdict": "approve", "findings": []}, True, id="approve_counts"),
        pytest.param({"verdict": "warn", "findings": []}, True, id="warn_counts"),
        pytest.param({"verdict": "block", "findings": [{"rule": "r"}]}, True, id="block_counts"),
        pytest.param(
            {"verdict": "needs-attention", "findings": [{"title": "T", "severity": "high"}]},
            True,
            id="needs_attention_counts",
        ),
        pytest.param({"verdict": "error"}, False, id="error_envelope_is_an_attempt"),
        pytest.param(None, False, id="unparseable_is_an_attempt"),
    ],
)
def test_outcome_is_the_single_round_vs_attempt_decision(payload, expect_round):
    """A real judgment must still COUNT — the fix must not swallow genuine rounds."""
    res = ar.outcome(payload, "raw output", "codex")
    assert (not res.get("no_verdict")) is expect_round


def test_one_severity_vocabulary():
    """It was written twice, 236 lines apart, free to drift."""
    src = (ar.REPO_ROOT / "tools" / "adversarial_review.py").read_text(encoding="utf-8")
    assert src.count('{"block", "critical", "high"}') == 1
    assert '{"critical", "high", "block"}' not in src, "a second, reordered copy came back"
