"""L1 — Unit tests for the playbook harvest collector (tools/playbook_harvest.py).

The harvest is the deterministic half of the weekly self-improvement loop. These
cover the part added with the review telemetry: the round count now comes from what
the reviewer actually recorded rather than from what an agent remembered to type,
and unreadable telemetry must not report a confident zero (CLAUDE.md rule 3).
"""

from __future__ import annotations

import json

import playbook_harvest as ph
import pytest

pytestmark = pytest.mark.unit


def _write(root, feature, records):
    d = root / ".harness" / "runs" / feature
    d.mkdir(parents=True, exist_ok=True)
    (d / "review.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8"
    )
    return d / "review.jsonl"


def _rec(verdict="block", rules=("x:y",)):
    return {
        "ts": "2026-08-08T00:00:00+00:00",
        "reviewer": "codex",
        "verdict": verdict,
        "n_findings": len(rules),
        "rules": list(rules),
        "blocking_rules": list(rules),
        "reviewer_note": "",
    }


def test_absent_telemetry_returns_none_not_zero(tmp_path, monkeypatch):
    """No file is 'unknown', not 'zero rounds' — the prose count must still win."""
    monkeypatch.setattr(ph, "ROOT", tmp_path)
    assert ph.review_telemetry("F-1") is None


def test_unreadable_telemetry_returns_none_rather_than_a_confident_count(tmp_path, monkeypatch):
    monkeypatch.setattr(ph, "ROOT", tmp_path)
    p = _write(tmp_path, "F-1", [_rec()])
    p.write_text("{not json\n", encoding="utf-8")
    assert ph.review_telemetry("F-1") is None


def test_empty_telemetry_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ph, "ROOT", tmp_path)
    _write(tmp_path, "F-1", [])
    assert ph.review_telemetry("F-1") is None


def test_telemetry_counts_rounds_and_unions_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(ph, "ROOT", tmp_path)
    _write(
        tmp_path,
        "F-1",
        [
            _rec(rules=("a:b",)),
            _rec(rules=("a:b", "c:d")),
            _rec(verdict="approve", rules=()),
        ],
    )
    t = ph.review_telemetry("F-1")
    assert t["count"] == 3
    assert t["rules"] == ["a:b", "c:d"]
    assert t["verdicts"] == ["block", "block", "approve"]


def test_parse_note_still_reads_the_prose_count_as_the_fallback():
    note = ph._parse_note(
        "Outcome: complete\nAdversarial rounds: 7\nPlaybook updates: none\n", "F-1", "p.md"
    )
    assert note.rounds == 7 and note.rounds_source == "note"
