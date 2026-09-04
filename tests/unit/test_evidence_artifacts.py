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
    # `head` mirrors what `cmd_critic` stamps — a verdict certifies the code it
    # judged, and `verify` fails closed on one it cannot place in history.
    for layer in ("deterministic", "judgment"):
        r = evidence.load_record(fid)
        r.setdefault("critic", {})[layer] = {"verdict": "approve", "head": evidence._head()}
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
    _seed_step(fid, 3, executed=True, exit_code=0)

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

    _seed_step(fid, 3, executed=True, exit_code=0)  # same head

    assert len(evidence.step_artifacts(evidence.load_record(fid), 3)) == 1


def test_verify_refuses_a_visual_close_on_stale_images(rec, tmp_path, monkeypatch):
    """An image from an earlier run must not satisfy the visual gate."""
    fid = rec(method="e2e")
    monkeypatch.setattr(evidence, "_head", lambda: "aaaaaaa")
    for n in (1, 2, 3, 4):
        _seed_step(fid, n, executed=True, exit_code=0)
    evidence.attach(fid, 3, _png(tmp_path), "the panel")

    # Re-stamp the AC step at a new head WITHOUT re-capturing, then force the stale
    # image to remain attached (the carry-forward would have dropped it).
    record = evidence.load_record(fid)
    stale = evidence.step_artifacts(record, 3)
    monkeypatch.setattr(evidence, "_head", lambda: "bbbbbbb")
    _seed_step(fid, 3, executed=True, exit_code=0)
    record = evidence.load_record(fid)
    next(s for s in record["steps"] if s["n"] == 3)["artifacts"] = stale
    evidence.save_record(fid, record)

    ok, problems, _ = evidence.verify(fid)

    assert ok is False
    assert any("earlier run" in p for p in problems), problems


# --- currency: agreeing with each other is not enough ------------------------
def test_verify_refuses_when_step_and_image_agree_but_both_are_stale(rec, tmp_path, monkeypatch):
    """Independent harness review [high].

    The first freshness fix only required `artifact.head == step.head`. A browser run
    and its screenshot captured TOGETHER on an older commit satisfy that trivially —
    both stale heads agree — so evidence from before a code change could still close
    the feature. Agreeing with each other is not the same as describing this code.
    """
    fid = rec(method="e2e")
    monkeypatch.setattr(evidence, "_head", lambda: "aaaaaaa")
    for n in (1, 2, 3, 4):
        _seed_step(fid, n, executed=True, exit_code=0)
    evidence.attach(fid, 3, _png(tmp_path), "the panel")
    # Internally consistent: step and image both say aaaaaaa.
    art = evidence.step_artifacts(evidence.load_record(fid), 3)[0]
    assert art["head"] == "aaaaaaa"

    # Code has since moved. Nothing about the record changed.
    monkeypatch.setattr(evidence, "code_changed_since", lambda h: ["python/atp_dashboard/app.js"])

    ok, problems, _ = evidence.verify(fid)

    assert ok is False
    assert any("code has changed since they were captured" in p for p in problems), problems


def test_verify_accepts_evidence_recorded_at_the_parent_of_its_own_chore_commit(
    rec, tmp_path, monkeypatch
):
    """The workflow this must NOT break.

    Evidence is recorded BEFORE the commit that carries it, so a valid record always
    names the parent of its own chore commit. Requiring equality with HEAD would
    reject every correctly-produced record — which is why the test is "has code
    moved", not "is the head current".
    """
    fid = rec(method="e2e")
    monkeypatch.setattr(evidence, "_head", lambda: "aaaaaaa")
    for n in (1, 2, 3, 4):
        _seed_step(fid, n, executed=True, exit_code=0)
    evidence.attach(fid, 3, _png(tmp_path), "the panel")
    record = evidence.load_record(fid)
    record["critic"] = {
        "deterministic": {"verdict": "approve"},
        "judgment": {"verdict": "approve"},
    }
    evidence.save_record(fid, record)

    # HEAD advanced by the evidence-only chore commit: no CODE moved.
    monkeypatch.setattr(evidence, "_head", lambda: "bbbbbbb")
    monkeypatch.setattr(evidence, "code_changed_since", lambda h: [])

    ok, problems, _ = evidence.verify(fid)

    assert ok is True, problems


def test_verify_fails_closed_when_the_recorded_commit_cannot_be_checked(rec, tmp_path, monkeypatch):
    """An unverifiable head is not a fresh one."""
    fid = rec(method="e2e")
    monkeypatch.setattr(evidence, "_head", lambda: "aaaaaaa")
    for n in (1, 2, 3, 4):
        _seed_step(fid, n, executed=True, exit_code=0)
    evidence.attach(fid, 3, _png(tmp_path), "the panel")

    monkeypatch.setattr(evidence, "code_changed_since", lambda h: None)

    ok, problems, _ = evidence.verify(fid)

    assert ok is False
    assert any("cannot be checked" in p for p in problems), problems


# --- code_changed_since, against real git ------------------------------------
@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A throwaway repo so the currency helper is tested, not mocked.

    The tests above monkeypatch `code_changed_since` to drive `verify`'s branches;
    that leaves the helper itself unexercised, and a mutation making it always report
    "fresh" survived every one of them. This fixture closes that.
    """
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args):
        sp.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    monkeypatch.setattr(evidence, "ROOT", repo)

    def commit(path, text):
        f = repo / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        run("add", "-A")
        run("commit", "-qm", f"touch {path}")
        return sp.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()

    return commit


def test_code_changed_since_is_empty_at_the_same_commit(git_repo):
    head = git_repo("src/app.py", "v1")

    assert evidence.code_changed_since(head) == []


def test_code_changed_since_ignores_evidence_only_commits(git_repo):
    head = git_repo("src/app.py", "v1")
    git_repo(".harness/runs/F-1/evidence.json", "{}")
    git_repo("progress.d/session-F-1.md", "note")

    # This is the workflow: record at the last code commit, then commit the record.
    assert evidence.code_changed_since(head) == []


def test_code_changed_since_reports_a_real_code_change(git_repo):
    head = git_repo("src/app.py", "v1")
    git_repo("src/app.py", "v2")

    assert evidence.code_changed_since(head) == ["src/app.py"]


@pytest.mark.parametrize(
    "head", [pytest.param("", id="empty"), pytest.param("deadbeefdeadbeef", id="unknown-sha")]
)
def test_code_changed_since_fails_closed_on_an_uncheckable_head(git_repo, head):
    git_repo("src/app.py", "v1")

    # None, never [] — an unverifiable head must not read as a fresh one.
    assert evidence.code_changed_since(head) is None


# --- a verdict certifies the code it judged ----------------------------------
# Steps and artifacts were bound to their commit; critic verdicts were not, so an
# `approve` recorded against one tree satisfied the gate against any later one —
# the same defect, one field over, in the dangerous direction. SRS-MD-003 is the
# live illustration in the safe direction: a `block` from 2026-08-09 against HEAD
# a8870cb, whose objection three later sessions addressed.
def _approve_at(fid, head):
    for layer in ("deterministic", "judgment"):
        r = evidence.load_record(fid)
        r.setdefault("critic", {})[layer] = {"verdict": "approve", "head": head}
        evidence.save_record(fid, r)


def test_an_approval_with_no_head_cannot_be_placed_in_history(rec, monkeypatch):
    """Rule 3: unknown is not fresh. Every pre-existing record is in this state,
    and each must be re-reviewed rather than grandfathered."""
    fid = rec(method="solo")
    for n in range(1, 5):
        _seed_step(fid, n)
    _approve_at(fid, None)
    ok, problems, _ = evidence.verify(fid, allow_attested=True)
    assert not ok
    assert any("an unrecorded commit" in p for p in problems)


def test_an_approval_whose_code_has_moved_is_rejected(rec, monkeypatch):
    fid = rec(method="solo")
    for n in range(1, 5):
        _seed_step(fid, n)
    _approve_at(fid, "cafebabe" * 5)
    monkeypatch.setattr(
        evidence, "code_changed_since", lambda h: ["crates/atp-market-data/src/lib.rs"]
    )
    ok, problems, _ = evidence.verify(fid, allow_attested=True)
    assert not ok
    assert any("non-evidence path(s) have changed" in p for p in problems)
    assert any("atp-market-data" in p for p in problems)  # names WHAT moved


def test_an_approval_that_cannot_be_checked_is_rejected(rec, monkeypatch):
    fid = rec(method="solo")
    for n in range(1, 5):
        _seed_step(fid, n)
    _approve_at(fid, "cafebabe" * 5)
    monkeypatch.setattr(evidence, "code_changed_since", lambda h: None)
    ok, problems, _ = evidence.verify(fid, allow_attested=True)
    assert not ok
    assert any("cannot be checked against this one" in p for p in problems)


def test_an_approval_passes_when_current_and_fails_once_code_moves(rec, monkeypatch):
    """Both directions of the same boundary, in one test deliberately.

    The accepting half alone cannot fail — before this change a stale approval
    passed too — so on its own it proves nothing (rule 6). It still has to be
    asserted: the guard must NOT reject the normal case, because evidence is
    recorded BEFORE the commit that carries it and a valid record always names the
    parent of its own chore commit.
    """
    fid = rec(method="solo")
    for n in range(1, 5):
        _seed_step(fid, n)
    _approve_at(fid, "cafebabe" * 5)

    monkeypatch.setattr(evidence, "code_changed_since", lambda h: [])
    ok, problems, _ = evidence.verify(fid, allow_attested=True)
    assert ok, problems

    monkeypatch.setattr(evidence, "code_changed_since", lambda h: ["crates/atp-types/src/lib.rs"])
    ok, problems, _ = evidence.verify(fid, allow_attested=True)
    assert not ok
    assert any("non-evidence path(s) have changed" in p for p in problems)


def test_a_block_is_reported_as_a_block_beside_a_currency_failure(rec, monkeypatch):
    """A blocking verdict must not be reframed as a staleness question — the
    operator needs to know the reviewer objected, not that a hash is old.

    Asserted alongside a REAL currency failure on the other layer, so the test
    exercises the new code path rather than only the pre-existing one: without the
    change the deterministic complaint never appears and this fails.
    """
    fid = rec(method="solo")
    for n in range(1, 5):
        _seed_step(fid, n)
    r = evidence.load_record(fid)
    r["critic"] = {
        "deterministic": {"verdict": "approve", "head": "cafebabe" * 5},
        "judgment": {"verdict": "block", "head": None},
    }
    evidence.save_record(fid, r)
    monkeypatch.setattr(evidence, "code_changed_since", lambda h: ["python/atp_dashboard/app.js"])
    ok, problems, _ = evidence.verify(fid, allow_attested=True)
    assert not ok
    # the objection, stated as an objection
    assert any("judgment critic verdict is 'block'" in p for p in problems)
    assert not any("judgment critic approved" in p for p in problems)
    # and the other layer's staleness, stated separately
    assert any("deterministic critic approved at cafebabe" in p for p in problems)


def test_cmd_critic_stamps_the_head_it_judged(rec, monkeypatch):
    """The recorder must supply what the gate now requires, or the honest path is
    unusable and everyone reaches for the override."""
    fid = rec(method="solo")
    monkeypatch.setattr(evidence, "_head", lambda: "d00df00d" * 5)

    class A:
        id, layer, verdict, reviewer, rounds = fid, "judgment", "approve", "codex", 3

    assert evidence.cmd_critic(A()) == 0
    got = evidence.load_record(fid)["critic"]["judgment"]
    assert got["head"] == "d00df00d" * 5
    assert got["verdict"] == "approve" and got["reviewer"] == "codex"


# --- The page must never contradict the record it renders -------------------
#
# `EVIDENCE.md` is the human-reviewable form of `evidence.json` (evidence.py:29),
# and it is the artifact a reviewer opens in the PR. A recorder that writes the
# record without re-rendering the page publishes a contradiction: the page said
# "critics: none recorded" while the record beside it, in the same commit, held a
# `block`. The reviewer reads "the critics never ran" instead of "the judgment
# critic blocked". These two tests pin the instance and the class.


def test_recording_a_critic_verdict_refreshes_the_page(rec, monkeypatch):
    """The instance: `critic` was the one recorder that left the page behind."""
    fid = rec(method="solo")
    for n in range(1, 5):
        _seed_step(fid, n)
    monkeypatch.setattr(evidence, "_head", lambda: "abc12345" * 5)
    evidence._write_markdown(fid)
    page = evidence.RUNS_DIR / fid / "EVIDENCE.md"
    assert "none recorded" in page.read_text(encoding="utf-8")

    class A:
        id, layer, verdict, reviewer, rounds = fid, "judgment", "block", "codex", 13

    assert evidence.cmd_critic(A()) == 0
    after = page.read_text(encoding="utf-8")
    assert "none recorded" not in after, "the page still denies a verdict the record holds"
    assert "block" in after


def test_every_recorder_that_saves_the_record_also_refreshes_the_page():
    """The class: this catches the NEXT recorder, not just the one that was wrong.

    Persistence is followed TRANSITIVELY. An earlier version keyed on a direct
    `save_record` call, which saw only `cmd_gate` and `cmd_critic` - while
    `run`, `record` and `artifact` persist through `_store_step`. So the guard
    was blind to the normal path: a new recorder written the ordinary way could
    omit the refresh and pass a check whose whole claim is "every writer of the
    source".
    """
    import ast
    import inspect

    # `cmd_gate` is the one legitimate exemption: `render_markdown` renders no
    # gate state at all, so a stale page cannot contradict the record about it.
    # The exemption is checked, not asserted - the moment gates reach the page it
    # expires by itself rather than quietly becoming wrong.
    exempt = {"cmd_gate": "render_markdown renders no gate state"}
    render_src = inspect.getsource(evidence.render_markdown)
    assert '"gates"' not in render_src and "'gates'" not in render_src, (
        "render_markdown now renders gate state, so cmd_gate can leave the page "
        "contradicting the record - remove it from `exempt` and refresh there too"
    )

    tree = ast.parse(inspect.getsource(evidence))
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def calls(node):
        return {
            n.func.id
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }

    # Close over "writes the record" until it stops growing, so a helper added
    # between a command and `save_record` cannot hide the command from this.
    persists = {"save_record"}
    while True:
        grown = {name for name, node in funcs.items() if calls(node) & persists}
        if grown <= persists:
            break
        persists |= grown

    assert "_store_step" in persists, (
        "the transitive walk no longer reaches _store_step; it would miss "
        "`run`, `record` and `artifact`, which is how this guard was too narrow"
    )

    offenders = [
        name
        for name, node in funcs.items()
        if name.startswith("cmd_")
        and name not in exempt
        and calls(node) & persists
        and not calls(node) & {"_refresh_markdown", "_write_markdown"}
    ]
    assert not offenders, (
        f"{offenders} write the evidence record but leave EVIDENCE.md stale; "
        "the page a reviewer opens would contradict the record beside it"
    )


# --- A transcript that promises captured output must not carry typed results -


def test_no_verification_transcript_asserts_a_result_it_did_not_run():
    """The worst defect this feature produced, made impossible to repeat.

    `.harness/runs/SRS-MD-005/VERIFICATION.md` opened with "Every block below is
    **captured terminal output**, not a summary" and then carried

        $ echo "cargo test --workspace : 176 suites ok, 0 failed"
        cargo test --workspace : 176 suites ok, 0 failed
        [exit 0]

    The `[exit 0]` is `echo`'s exit code. The number happened to be right, which
    is exactly why a reader could not tell: a typed result and a captured one
    look identical once they are on the page. So the shape is banned outright
    rather than spot-checked - an `echo` may report a real exit code it captured
    (`; echo "... exit $?"`), but it may never state a result of its own.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    runs = root / ".harness" / "runs"
    if not runs.is_dir():
        pytest.skip("no evidence runs in this tree")

    # A result claim: a verdict word or a count, in text `echo` invents rather
    # than reads. `$?` and `{}` mean the value came from a command that ran.
    claim = re.compile(
        r"\b(ok|pass(ed)?|fail(ed|ures)?|green|clean|\d+\s*(suites?|tests?))\b", re.I
    )
    offenders = []
    for doc in sorted(runs.rglob("*.md")):
        text = doc.read_text(encoding="utf-8", errors="replace")
        if "captured terminal output" not in text:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            m = re.match(r"\$\s*echo\s+(['\"])(.*)\1\s*$", line.strip())
            if m and claim.search(m.group(2)) and "$?" not in m.group(2) and "{}" not in m.group(2):
                offenders.append(f"{doc.relative_to(root)}:{n}: {line.strip()}")
    assert not offenders, "typed result(s) under a promise of captured output:\n  " + "\n  ".join(
        offenders
    )
