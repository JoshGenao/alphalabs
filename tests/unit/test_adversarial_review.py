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
