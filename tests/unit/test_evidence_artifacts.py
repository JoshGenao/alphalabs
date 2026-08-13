"""L1 — Closure artifacts: the part of the evidence a human can look at.

A captured exit code proves a command ran. It cannot show that the dashboard
displayed the stale row — and "the dashboard shows IB equity, daily and cumulative
P&L, margin usage" is an acceptance criterion about exactly that. These tests pin
the gate (an e2e/live-ib feature cannot close without an image), the size
discipline (this repo has no git-lfs), and the rendering GitHub actually displays.
"""

import json

import evidence
import pytest

pytestmark = pytest.mark.unit

# The smallest valid PNG: 1x1, RGBA, one IDAT chunk.
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154"
    "789c63000100000500010d0a2db40000000049454e44ae426082"
)

AC_TEXT = "Step 3: Verify acceptance criteria: the panel shows equity and P&L."


@pytest.fixture
def rec(tmp_path, monkeypatch):
    """An isolated record for a 4-step feature whose method the test picks."""
    feats = tmp_path / "feature_list.json"
    monkeypatch.setattr(evidence, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(evidence, "FEATURE_FILE", feats)

    def _make(method="e2e", fid="F-1"):
        feats.write_text(
            json.dumps(
                [
                    {
                        "id": fid,
                        "passes": False,
                        "verification_method": method,
                        "description": "a feature",
                        "steps": ["Step 1: init", "Step 2: exercise", AC_TEXT, "Step 4: record"],
                    }
                ]
            ),
            encoding="utf-8",
        )
        return fid

    return _make


def _seed_step(fid, n, **kw):
    entry = {
        "n": n,
        "step_text": "t",
        "command": "manual",
        "observed": "looked at it",
        "status": "pass",
        "executed": False,
    }
    entry.update(kw)
    evidence._store_step(fid, n, entry)


def _png(tmp_path, name="shot.png", size=None):
    p = tmp_path / name
    p.write_bytes(TINY_PNG if size is None else b"\x89PNG" + b"\0" * size)
    return p


# --- attach -----------------------------------------------------------------
def test_attach_records_the_artifact_against_its_step(rec, tmp_path):
    fid = rec()
    _seed_step(fid, 3)
    art = evidence.attach(fid, 3, _png(tmp_path), "the panel")
    assert art["kind"] == "image"
    stored = evidence.step_artifacts(evidence.load_record(fid), 3)
    assert [a["name"] for a in stored] == ["step3-shot.png"]
    assert stored[0]["caption"] == "the panel"
    assert (evidence.artifacts_dir(fid) / "step3-shot.png").is_file()


def test_attach_refuses_a_step_that_has_no_record(rec, tmp_path):
    """An artifact with no step is evidence of nothing in particular."""
    fid = rec()
    with pytest.raises(evidence.EvidenceError, match="no record yet"):
        evidence.attach(fid, 3, _png(tmp_path), "orphan")


def test_attach_refuses_an_oversized_image_rather_than_truncating(rec, tmp_path):
    """Refuse, never silently drop: a record claiming a screenshot with no
    screenshot on disk is the rule-3 failure applied to the write side."""
    fid = rec()
    _seed_step(fid, 3)
    big = _png(tmp_path, "big.png", size=evidence.MAX_IMAGE_BYTES + 1)
    with pytest.raises(evidence.EvidenceError, match="cap for a image"):
        evidence.attach(fid, 3, big, "too big")
    assert evidence.step_artifacts(evidence.load_record(fid), 3) == []


def test_attach_refuses_an_unknown_file_type(rec, tmp_path):
    fid = rec()
    _seed_step(fid, 3)
    blob = tmp_path / "thing.bin"
    blob.write_bytes(b"\0\1\2")
    with pytest.raises(evidence.EvidenceError, match="not an artifact type"):
        evidence.attach(fid, 3, blob, "mystery")


def test_attach_enforces_a_per_feature_total(rec, tmp_path, monkeypatch):
    """Every byte here is permanent history for every future clone."""
    monkeypatch.setattr(evidence, "MAX_TOTAL_BYTES", 300)
    monkeypatch.setattr(evidence, "MAX_IMAGE_BYTES", 200)
    fid = rec()
    _seed_step(fid, 3)
    evidence.attach(fid, 3, _png(tmp_path, "a.png", size=150), "one")
    with pytest.raises(evidence.EvidenceError, match="per-feature cap"):
        evidence.attach(fid, 3, _png(tmp_path, "b.png", size=150), "two")


def test_reattaching_the_same_name_replaces_rather_than_duplicates(rec, tmp_path):
    fid = rec()
    _seed_step(fid, 3)
    evidence.attach(fid, 3, _png(tmp_path), "first take")
    evidence.attach(fid, 3, _png(tmp_path), "second take")
    stored = evidence.step_artifacts(evidence.load_record(fid), 3)
    assert len(stored) == 1 and stored[0]["caption"] == "second take"


# --- the AC step is found by text, not by position --------------------------
def test_ac_step_index_matches_the_text_not_the_position():
    """The 4-step shape is a generator template, not a guarantee. Hard-coding 3
    would apply the artifact requirement to the wrong step of a hand-written one."""
    assert evidence.ac_step_index(["a", AC_TEXT, "c"]) == 2
    assert evidence.ac_step_index(["a", "b"]) is None


# --- the gate ---------------------------------------------------------------
def _complete_record(fid, *, steps=4):
    for n in range(1, steps + 1):
        _seed_step(fid, n)
    for layer in ("deterministic", "judgment"):
        r = evidence.load_record(fid)
        r.setdefault("critic", {})[layer] = {"verdict": "approve"}
        evidence.save_record(fid, r)


def test_an_e2e_feature_cannot_verify_without_an_image(rec):
    fid = rec(method="e2e")
    _complete_record(fid)
    ok, problems, _ = evidence.verify(fid, allow_attested=True)
    assert not ok
    assert any("no image artifact" in p for p in problems)


def test_the_same_e2e_feature_verifies_once_an_image_is_attached(rec, tmp_path):
    fid = rec(method="e2e")
    _complete_record(fid)
    evidence.attach(fid, 3, _png(tmp_path), "the panel")
    ok, problems, summary = evidence.verify(fid, allow_attested=True)
    assert ok, problems
    assert summary["artifacts"] == 1


def test_a_live_ib_feature_is_gated_the_same_way(rec):
    fid = rec(method="live-ib")
    _complete_record(fid)
    ok, problems, _ = evidence.verify(fid, allow_attested=True)
    assert any("no image artifact" in p for p in problems)


@pytest.mark.parametrize("method", ["solo", "integration"], ids=["m_solo", "m_integration"])
def test_a_non_visual_feature_needs_no_image(rec, method):
    """For these the captured stdout IS the artifact. Demanding a screenshot of
    `cargo fmt --check` teaches everyone to produce a meaningless one.

    Asserts the artifact COUNT as well as the verdict, not just `ok`. Checking only
    `ok` passed before this change existed too, so it proved nothing about the
    gate's scope — the guard has to show the artifact machinery ran and chose not
    to fire, which is a different fact from it never having run.
    """
    fid = rec(method=method)
    _complete_record(fid)
    ok, problems, summary = evidence.verify(fid, allow_attested=True)
    assert ok, problems
    assert summary["artifacts"] == 0


def test_a_video_alone_does_not_satisfy_the_image_gate(rec, tmp_path):
    """GitHub does not play video from a repo path, so a video-only record is one
    a reviewer cannot actually review in the PR."""
    fid = rec(method="e2e")
    _complete_record(fid)
    vid = tmp_path / "run.webm"
    vid.write_bytes(b"\x1a\x45\xdf\xa3" + b"\0" * 32)
    evidence.attach(fid, 3, vid, "session recording")
    ok, problems, _ = evidence.verify(fid, allow_attested=True)
    assert not ok
    assert any("no image artifact" in p for p in problems)


# --- rendering --------------------------------------------------------------
def test_render_embeds_images_inline_and_only_links_video(rec, tmp_path):
    fid = rec(method="e2e")
    _complete_record(fid)
    evidence.attach(fid, 3, _png(tmp_path), "the account panel")
    vid = tmp_path / "run.webm"
    vid.write_bytes(b"\x1a\x45\xdf\xa3" + b"\0" * 32)
    evidence.attach(fid, 3, vid, "session recording")
    md = evidence.render_markdown(fid)
    assert "![the account panel](artifacts/step3-shot.png)" in md
    assert "![session recording]" not in md  # video is NOT embedded
    assert "(artifacts/step3-run.webm)" in md
    assert "does not" in md and "play video" in md  # and the reader is told why


def test_render_survives_an_unreadable_record(rec):
    fid = rec()
    evidence.record_path(fid).parent.mkdir(parents=True, exist_ok=True)
    evidence.record_path(fid).write_text("{not json", encoding="utf-8")
    md = evidence.render_markdown(fid)
    assert "unreadable" in md  # a report, not a traceback


# --- retirement -------------------------------------------------------------
def test_retire_archives_the_artifacts_with_the_record(rec, tmp_path):
    """A reopened feature must not start with the PREVIOUS session's screenshot
    sitting where its own evidence belongs — a stale screenshot is more convincing
    than a stale exit code, not less."""
    fid = rec(method="e2e")
    _seed_step(fid, 3)
    evidence.attach(fid, 3, _png(tmp_path), "the panel")
    evidence._write_markdown(fid)
    assert evidence.artifacts_dir(fid).is_dir()

    evidence.retire(fid)
    assert not evidence.artifacts_dir(fid).exists()
    assert not (evidence.RUNS_DIR / fid / "EVIDENCE.md").exists()
    archived = list((evidence.RUNS_DIR / fid).glob("closed-*-artifacts"))
    assert len(archived) == 1 and (archived[0] / "step3-shot.png").is_file()
    assert list((evidence.RUNS_DIR / fid).glob("closed-*-EVIDENCE.md"))


# --- freshness: an artifact certifies the code it was captured on --------------
def test_an_artifact_records_the_head_it_was_captured_at(rec, tmp_path):
    fid = rec()
    _seed_step(fid, 3)

    art = evidence.attach(fid, 3, _png(tmp_path), "the panel")

    # A screenshot is a claim about what a reviewer would see running specific code,
    # so it has to carry which code.
    assert art["head"] == evidence._head()


def test_a_rerun_at_a_new_head_does_not_retain_the_previous_images(rec, tmp_path, monkeypatch):
    """Round-16 adversarial review [high].

    `_store_step` carries artifacts forward so a browser test's screenshots survive
    the `run` that produced them. Left unqualified, that let a LATER passing run
    stamp the step at a new commit while older images rode along — the visual gate
    then certifying a dashboard nobody had looked at on this code.
    """
    fid = rec()
    monkeypatch.setattr(evidence, "_head", lambda: "aaaaaaa")
    _seed_step(fid, 3)
    evidence.attach(fid, 3, _png(tmp_path), "captured on aaaaaaa")
    assert len(evidence.step_artifacts(evidence.load_record(fid), 3)) == 1

    # A second successful run at a DIFFERENT head that captured nothing.
    monkeypatch.setattr(evidence, "_head", lambda: "bbbbbbb")
    _seed_step(fid, 3, executed=True)

    assert evidence.step_artifacts(evidence.load_record(fid), 3) == [], (
        "images from an earlier commit must not ride along onto a fresh run"
    )


def test_a_rerun_at_the_same_head_keeps_the_images_it_produced(rec, tmp_path, monkeypatch):
    """The other direction, so the fix is not "drop everything always": a browser
    test attaches from INSIDE the subprocess `run` is executing, and those artifacts
    must survive the entry `run` writes immediately afterwards."""
    fid = rec()
    monkeypatch.setattr(evidence, "_head", lambda: "aaaaaaa")
    _seed_step(fid, 3)
    evidence.attach(fid, 3, _png(tmp_path), "captured on aaaaaaa")

    _seed_step(fid, 3, executed=True)  # same head

    assert len(evidence.step_artifacts(evidence.load_record(fid), 3)) == 1


def test_verify_refuses_a_visual_close_on_stale_images(rec, tmp_path, monkeypatch):
    """An image from an earlier run must not satisfy the visual gate."""
    fid = rec(method="e2e")
    monkeypatch.setattr(evidence, "_head", lambda: "aaaaaaa")
    for n in (1, 2, 3, 4):
        _seed_step(fid, n, executed=True)
    evidence.attach(fid, 3, _png(tmp_path), "the panel")

    # Re-stamp the AC step at a new head WITHOUT re-capturing, then force the stale
    # image to remain attached (the carry-forward would have dropped it).
    record = evidence.load_record(fid)
    stale = evidence.step_artifacts(record, 3)
    monkeypatch.setattr(evidence, "_head", lambda: "bbbbbbb")
    _seed_step(fid, 3, executed=True)
    record = evidence.load_record(fid)
    next(s for s in record["steps"] if s["n"] == 3)["artifacts"] = stale
    evidence.save_record(fid, record)

    ok, problems, _ = evidence.verify(fid)

    assert ok is False
    assert any("earlier run" in p for p in problems), problems
