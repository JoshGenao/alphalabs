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
