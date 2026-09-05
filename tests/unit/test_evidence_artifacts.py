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


# Commands whose success proves nothing, so `<noop> && echo "all green"` is a
# fabrication wearing the shape of a conditional report.
_NOOP_COMMANDS = frozenset({"true", ":", "echo", "printf", "test", "["})


def _fabricated_result_claims(text: str) -> list[tuple[int, str]]:
    r"""Lines that PRINT a result rather than capture one.

    A print is legitimate when the value comes from a command that ran - `$?`, an
    xargs placeholder, a substitution, a `%d` conversion - or when a command that
    could actually have produced that claim gates it with `&&`.

    Both halves were learned the hard way. Keying on `^\$ echo "` missed
    `printf`, a trailing `echo`, and `true && echo "all green"`. Accepting ANY
    non-no-op gate then let `ls && echo "176 suites ok"` through: the gate is
    real, and it proves nothing about tests. So the gate must also be ABOUT the
    claim - at least one substantial word of what is printed has to appear in
    the command being gated.
    """
    import re

    claim = re.compile(
        r"\b(ok|pass(ed)?|fail(ed|ures)?|green|clean|success|\d+\s*(suites?|tests?))\b",
        re.I,
    )
    # Quoted OR bare: `echo "176 suites ok"` and `echo 176 suites ok` are the
    # same statement, and only one of them was being read.
    # `echo`, `printf` AND `print(...)`. The guarded transcript's own Section 6
    # uses `python -c "... print(...)"`, so a matcher keyed on shell builtins was
    # narrower than the class its docstring claimed - it could not see the very
    # idiom the document it guards is written in.
    # Every way to put text on a terminal that this repo's transcripts use.
    # Keying on three printers was still keying on SHAPE: `sys.stdout.write`, a
    # `cat <<EOF` heredoc and a `tee <<<` here-string each walked past a guard
    # whose docstring claims to read "what is PRINTED".
    printer = re.compile(
        r"\b(?:echo|printf)\s+(?:(['\"])(.*?)\1|([^|;&]+))"
        r"|\b(?:print|write|puts)\s*\(\s*(['\"])(.*?)\4\s*\)"
        r"|<<<\s*(['\"])(.*?)\6"
    )
    derived = ("$?", "{}", "$(", "`")
    out = []
    in_command = False
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith("$"):
            in_command = True
            cmd = line[1:].strip()
        elif in_command and line and not line.startswith("[exit "):
            # A multi-line command BODY. Skipping every line that did not start
            # with `$` made a typed result inside one invisible - and the
            # multi-line `python -c "..."` block in Section 6 is the only
            # `python -c` the guarded transcript actually contains, so the guard
            # was blind to the exact place the idiom appears.
            cmd = line
        else:
            in_command = False
            continue
        for m in printer.finditer(cmd):
            printed = next(
                (g for g in (m.group(2), m.group(3), m.group(5), m.group(7)) if g), ""
            ).strip()
            if not claim.search(printed):
                continue
            if any(tok in printed for tok in derived):
                continue
            if re.search(r"%[-+ #0-9.]*[difsuxXeEgGc]", printed):
                continue
            before = cmd[: m.start()]
            if "&&" not in before:
                out.append((n, line))
                continue
            gate = before.rsplit("&&", 1)[0].strip()
            head = gate.split()[0] if gate else ""
            if head in _NOOP_COMMANDS or not head:
                out.append((n, line))
                continue
            # The gate must be about the claim. Prefix-match so "builds" in the
            # text finds "build" in `cargo build`.
            words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", printed)]
            gate_l = gate.lower()
            if not any(w[:5] in gate_l for w in words):
                out.append((n, line))
    return out


def test_no_verification_transcript_asserts_a_result_it_did_not_run():
    """The worst defect this feature produced, made impossible to repeat.

    `.harness/runs/SRS-MD-005/VERIFICATION.md` opened with "Every block below is
    **captured terminal output**, not a summary" and then carried

        $ echo "cargo test --workspace : 176 suites ok, 0 failed"
        cargo test --workspace : 176 suites ok, 0 failed
        [exit 0]

    The `[exit 0]` is `echo`'s exit code. The number happened to be right, which
    is exactly why a reader could not tell: a typed result and a captured one
    look identical once they are on the page.

    The first version of this guard matched only a line beginning `$ echo "`,
    which `printf`, a trailing `echo`, or `true && echo "all green"` walks
    straight past - and the guarded document already carried two `&& echo`
    lines. It now reads what is PRINTED and asks where the value came from.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    runs = root / ".harness" / "runs"
    if not runs.is_dir():
        pytest.skip("no evidence runs in this tree")

    offenders = []
    for doc in sorted(runs.rglob("*.md")):
        text = doc.read_text(encoding="utf-8", errors="replace")
        if "captured terminal output" not in text:
            continue
        offenders += [
            f"{doc.relative_to(root)}:{n}: {line}" for n, line in _fabricated_result_claims(text)
        ]
    assert not offenders, "typed result(s) under a promise of captured output:\n  " + "\n  ".join(
        offenders
    )


@pytest.mark.parametrize(
    ("label", "line", "fabricated"),
    [
        ("bare echo", '$ echo "cargo test --workspace : 176 suites ok, 0 failed"', True),
        ("printf instead", '$ printf "176 suites ok"', True),
        ("no-op gate", '$ true && echo "176 suites ok"', True),
        ("echo gating echo", '$ echo hi && echo "all tests passed"', True),
        ("real command gate", "$ cargo build -q && echo 'restored: builds clean again'", False),
        ("unquoted echo", "$ echo cargo test --workspace : 176 suites ok, 0 failed", True),
        (
            "python -c print",
            "$ .venv/bin/python -c \"print('176 suites ok, 0 failed')\"",
            True,
        ),
        (
            # The shape Section 6 actually uses: the claim is on a CONTINUATION
            # line, which the first version of this matcher never read.
            "multi-line command body",
            "$ .venv/bin/python -c \"\nprint('176 suites ok, 0 failed')\n\"",
            True,
        ),
        (
            "sys.stdout.write",
            "$ .venv/bin/python -c \"import sys; sys.stdout.write('176 suites ok')\"",
            True,
        ),
        (
            "here-string",
            "$ tee out.txt <<< '176 suites ok, 0 failed'",
            True,
        ),
        (
            "multi-line body reading a value",
            "$ .venv/bin/python -c \"\nimport json\nprint(json.load(open('e.json'))['n'])\n\"",
            False,
        ),
        (
            # The honest shape of the same idiom: values read from the record.
            "python -c printing a read value",
            "$ .venv/bin/python -c \"import json; print(json.load(open('e.json'))['ok'])\"",
            False,
        ),
        ("gate real but irrelevant", '$ ls && echo "176 suites ok, 0 failed"', True),
        (
            "gate about the claim",
            "$ git merge-base --is-ancestor a b && echo 'merge-base check passed'",
            False,
        ),
        ("captured exit code", '$ cargo clippy > /dev/null; echo "clippy : exit $?"', False),
        (
            "xargs placeholder",
            "$ cargo fmt --check | xargs -I{} echo '{} files need reformatting'",
            False,
        ),
        ("not a result claim", '$ echo "--- SECTION 9 ---"', False),
        (
            "awk printf over a real pipeline",
            "$ cargo test --workspace | awk 'END {printf \"%d suites, %d ok\\n\", s, ok}'",
            False,
        ),
        ("output line, not a command", "176 suites, 176 ok, 0 failed", False),
    ],
)
def test_the_fabrication_matcher_separates_typed_results_from_captured_ones(
    label, line, fabricated
):
    """Every bypass the reviewer named, plus the honest shapes it must not break."""
    hits = _fabricated_result_claims(line)
    assert bool(hits) is fabricated, f"{label}: {line!r} -> {hits}"


def test_verify_rejects_a_transcript_captured_before_the_code_moved(rec, monkeypatch):
    """Round 16: a transcript presented captures from a superseded tree.

    `VERIFICATION.md` said its commands "were re-run against the integrated tree
    (`ed36c790`)" while the diff shipping it had rewritten 215 lines of the
    module those captures cover. The captures were real; they certified
    different code, which a reader cannot see.

    This lives in `verify` - the CLOSE gate - and not in an assertion over the
    live repo. Evidence is committed AFTER the code it describes, so a test
    asserting on live evidence state is red at every code commit by
    construction. The first version of this check was exactly that, and it
    turned CI red on the commit that introduced it.
    """
    fid = rec(method="solo")
    run = evidence.RUNS_DIR / fid
    run.mkdir(parents=True, exist_ok=True)
    (run / "VERIFICATION.md").write_text(
        "$ git rev-parse HEAD\n" + "a" * 40 + "\n[exit 0]\n", encoding="utf-8"
    )

    monkeypatch.setattr(evidence, "code_changed_since", lambda head: ["crates/x/src/lib.rs"])
    problems = evidence.record_self_consistency_problems(fid)
    assert any("code path(s) have changed since" in p for p in problems), problems

    monkeypatch.setattr(evidence, "code_changed_since", lambda head: [])
    assert not evidence.record_self_consistency_problems(fid)


def test_verify_fails_closed_when_the_transcript_cannot_be_placed_in_history(rec, monkeypatch):
    """Unknown is not fresh (CLAUDE.md rule 3)."""
    fid = rec(method="solo")
    run = evidence.RUNS_DIR / fid
    run.mkdir(parents=True, exist_ok=True)
    (run / "VERIFICATION.md").write_text(
        "$ git rev-parse HEAD\n" + "b" * 40 + "\n[exit 0]\n", encoding="utf-8"
    )
    monkeypatch.setattr(evidence, "code_changed_since", lambda head: None)
    assert any("cannot be checked" in p for p in evidence.record_self_consistency_problems(fid))


def test_verify_rejects_a_transcript_with_no_captured_head(rec):
    """A transcript that names no commit cannot be checked, so it is not clean."""
    fid = rec(method="solo")
    run = evidence.RUNS_DIR / fid
    run.mkdir(parents=True, exist_ok=True)
    (run / "VERIFICATION.md").write_text("no provenance here\n", encoding="utf-8")
    assert any(
        "records no captured HEAD" in p for p in evidence.record_self_consistency_problems(fid)
    )


@pytest.mark.parametrize(
    ("stamped", "ledger_verdicts", "flagged"),
    [
        (15, ["block"] * 16, True),
        (16, ["block"] * 16, False),
        # Attempts that produced NO verdict are not rounds, so 15 blocks plus
        # two failed attempts is 15 - which is why the count cannot be
        # "lines in the file".
        (15, ["block"] * 15 + ["none", "none"], False),
        (17, ["block"] * 15 + ["none", "none"], True),
        (3, ["approve", "warn", "block"], False),
    ],
)
def test_verify_checks_the_stamped_round_count_against_the_ledger(
    stamped, ledger_verdicts, flagged, rec
):
    """`--rounds N` takes whatever the caller types, and the caller was me.

    It read 15 while `review.jsonl` held 16, and the guard checking documents
    against that field passed - a guard anchored to an unverified number
    certifies the drift instead of catching it. Note the third case: attempts
    that produced NO verdict are not rounds, which is why the count cannot be
    "lines in the file".
    """
    fid = rec(method="solo")
    run = evidence.RUNS_DIR / fid
    run.mkdir(parents=True, exist_ok=True)
    (run / "review.jsonl").write_text(
        "\n".join(json.dumps({"verdict": v}) for v in ledger_verdicts) + "\n", encoding="utf-8"
    )
    r = evidence.load_record(fid)
    r.setdefault("critic", {})["judgment"] = {"verdict": "block", "rounds": stamped}
    evidence.save_record(fid, r)

    problems = evidence.record_self_consistency_problems(fid)
    assert bool([p for p in problems if "rounds" in p]) is flagged, problems


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ("| **F-1** | judgment is `block` after 13 rounds |", True),
        ("| **F-1** | judgment is `block` after 16 rounds |", False),
        ("Adversarial rounds: 13 (plus failed attempts)", True),
        ("Adversarial rounds: 16 (plus failed attempts)", False),
        # An ORDINAL is narration, not a total. It stays true forever, and
        # matching it raised 38 accusations against session notes doing nothing
        # wrong on this guard's first run.
        ("  r12 warn/3 - found the precedence defect", False),
        ("Rounds 1-4 found defects in the FEATURE.", False),
    ],
)
def test_only_a_stated_round_TOTAL_is_checked_against_the_ledger(line, flagged, rec, tmp_path):
    """Totals go stale; ordinals do not."""
    fid = rec(method="solo")
    run = evidence.RUNS_DIR / fid
    run.mkdir(parents=True, exist_ok=True)
    (run / "review.jsonl").write_text(
        "\n".join(json.dumps({"verdict": "block"}) for _ in range(16)) + "\n", encoding="utf-8"
    )
    note = evidence.ROOT / "progress.d" / f"session-{fid}.md"
    assert not note.exists(), "this fixture must not touch a real session note"
    (run / "VERIFICATION.md").write_text(line + "\n", encoding="utf-8")

    problems = [p for p in evidence._round_count_drift(fid, 16)]
    assert bool(problems) is flagged, (line, problems)


@pytest.mark.parametrize(
    ("label", "critic", "row", "flagged"),
    [
        ("block hidden", {"judgment": {"verdict": "block"}}, "Nothing outstanding.", True),
        (
            # Both layers present, one blocked and named. The earlier version of
            # this case recorded only a judgment entry, which now correctly
            # flags the MISSING deterministic verdict as well - that record
            # cannot close either.
            "block disclosed",
            {"deterministic": {"verdict": "approve"}, "judgment": {"verdict": "block"}},
            "judgment is `block`.",
            False,
        ),
        ("no judgment entry", {"deterministic": {"verdict": "approve"}}, "Nothing.", True),
        ("no critic block", None, "Nothing.", True),
        (
            # "unblocks" contains "block". A substring test accepted this row.
            "prose containing the word",
            {"judgment": {"verdict": "block"}},
            "This unblocks SRS-SAFE-003.",
            True,
        ),
        (
            "both approve",
            {"judgment": {"verdict": "approve"}, "deterministic": {"verdict": "approve"}},
            "Nothing outstanding.",
            False,
        ),
    ],
)
def test_a_queue_row_promising_a_close_must_disclose_a_standing_verdict(
    label, critic, row, flagged, rec, tmp_path, monkeypatch
):
    """`--attested-by` relaxes which STEPS count, never the critic gate.

    A row saying "Nothing" is missing beside a `block` sends the operator to
    `close_feature.py`, which exits 3. Absent, unreadable and unknown are each
    fatal here, not empty (CLAUDE.md rule 3): `verify` needs BOTH layers to
    approve, so a record with no judgment entry cannot close either.
    """
    fid = rec(method="solo")
    r = evidence.load_record(fid)
    if critic is None:
        r.pop("critic", None)
    else:
        r["critic"] = critic
    evidence.save_record(fid, r)

    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "verification-queue.md").write_text(
        f"| **{fid}** | 1 | {row} | `close_feature.py {fid} --verified` |\n", encoding="utf-8"
    )
    monkeypatch.setattr(evidence, "ROOT", tmp_path)
    assert bool(evidence._queue_row_problems(fid)) is flagged, label


@pytest.mark.parametrize(
    ("label", "write_ledger", "content"),
    [
        ("absent", False, None),
        ("unparseable", True, "{not json\n"),
    ],
)
def test_a_round_count_with_no_readable_ledger_behind_it_is_not_consistent(
    label, write_ledger, content, rec
):
    """CLAUDE.md rule 3, applied to the corroborating artifact.

    The check was guarded by `if ledger.exists()`, so a hand-typed
    `evidence.py critic --rounds N` stamped without the reviewer ever having run
    produced ZERO problems - the exact case the check exists to catch. Absent
    and unreadable each need their own fail-closed state.
    """
    fid = rec(method="solo")
    run = evidence.RUNS_DIR / fid
    run.mkdir(parents=True, exist_ok=True)
    if write_ledger:
        (run / "review.jsonl").write_text(content, encoding="utf-8")
    r = evidence.load_record(fid)
    r.setdefault("critic", {})["judgment"] = {"verdict": "block", "rounds": 12}
    evidence.save_record(fid, r)

    problems = evidence.record_self_consistency_problems(fid)
    assert any("cannot be corroborated" in p for p in problems), (label, problems)


def test_a_record_with_no_round_count_needs_no_ledger(rec):
    """Non-vacuity: the demand is on the CLAIM, not on every record.

    Without this, the fix above could have been "always require a ledger", which
    would fail every feature that never recorded a round count.
    """
    fid = rec(method="solo")
    r = evidence.load_record(fid)
    r.setdefault("critic", {})["judgment"] = {"verdict": "block"}
    evidence.save_record(fid, r)
    assert not [p for p in evidence.record_self_consistency_problems(fid) if "corroborated" in p]


def test_re_stamping_a_verdict_without_rounds_keeps_the_count(rec, monkeypatch):
    """`cmd_critic` builds a FRESH entry, so omitting --rounds erased the stamp.

    That silently disabled the corroboration check against review.jsonl, which
    only compares counts that are PRESENT - so the routine re-stamp prescribed
    by the verification queue turned off the guard the same change had added.
    The count belongs to the ledger, not to whoever typed the last command.
    """
    fid = rec(method="solo")
    monkeypatch.setattr(evidence, "_head", lambda: "c0ffee12" * 5)

    class WithRounds:
        id, layer, verdict, reviewer, rounds = fid, "judgment", "block", "codex", 18

    class WithoutRounds:
        id, layer, verdict, reviewer, rounds = fid, "judgment", "approve", "codex", None

    assert evidence.cmd_critic(WithRounds()) == 0
    assert evidence.load_record(fid)["critic"]["judgment"]["rounds"] == 18

    assert evidence.cmd_critic(WithoutRounds()) == 0
    after = evidence.load_record(fid)["critic"]["judgment"]
    assert after["verdict"] == "approve", "the new verdict must still land"
    assert after["rounds"] == 18, "the round count must survive a re-stamp"


def test_omitting_rounds_reads_the_ledger_rather_than_the_previous_stamp(rec, monkeypatch):
    """Carrying the previous stamp forward could not work, and the queue proved it.

    The round that produces an `approve` appends its own line to review.jsonl,
    so the documented close recipe - re-stamp without `--rounds` - wrote N
    against a ledger of N+1 and `verify` refused. The recipe was unrunnable and
    the tool was the reason. The count belongs to the ledger.
    """
    fid = rec(method="solo")
    monkeypatch.setattr(evidence, "_head", lambda: "beefcafe" * 5)
    run = evidence.RUNS_DIR / fid
    run.mkdir(parents=True, exist_ok=True)

    class Stamp:
        id, layer, verdict, reviewer, rounds = fid, "judgment", "block", "codex", 20

    assert evidence.cmd_critic(Stamp()) == 0

    # The approving round appends its own verdict line.
    (run / "review.jsonl").write_text(
        "\n".join(json.dumps({"verdict": "block"}) for _ in range(20))
        + "\n"
        + json.dumps({"verdict": "approve"})
        + "\n",
        encoding="utf-8",
    )

    class Approve:
        id, layer, verdict, reviewer, rounds = fid, "judgment", "approve", "codex", None

    assert evidence.cmd_critic(Approve()) == 0
    after = evidence.load_record(fid)["critic"]["judgment"]
    assert after["rounds"] == 21, "the stamp must follow the ledger, not the previous stamp"
    assert not [p for p in evidence.record_self_consistency_problems(fid) if "rounds" in p]


def test_an_explicit_rounds_value_still_wins(rec, monkeypatch):
    """Non-vacuity: carrying forward must not make --rounds inert."""
    fid = rec(method="solo")
    monkeypatch.setattr(evidence, "_head", lambda: "c0ffee12" * 5)

    class A:
        id, layer, verdict, reviewer, rounds = fid, "judgment", "block", "codex", 18

    class B:
        id, layer, verdict, reviewer, rounds = fid, "judgment", "block", "codex", 19

    evidence.cmd_critic(A())
    evidence.cmd_critic(B())
    assert evidence.load_record(fid)["critic"]["judgment"]["rounds"] == 19


def test_a_stale_rendered_page_is_a_close_time_problem(rec):
    """The page is the artifact a reviewer opens, so it must match the record.

    `cmd_critic` re-renders, but anything written AFTER it - a re-captured
    transcript, a later verify - leaves the page behind again. The page
    published two outstanding problems that no longer existed and named a
    transcript revision the transcript beside it had moved past. Comparing it to
    `render_markdown` needs no memory of when to re-run.
    """
    fid = rec(method="solo")
    for n in range(1, 5):
        _seed_step(fid, n)
    evidence._write_markdown(fid)
    assert not [p for p in evidence.record_self_consistency_problems(fid) if "stale" in p]

    page = evidence.RUNS_DIR / fid / "EVIDENCE.md"
    page.write_text(page.read_text(encoding="utf-8") + "\ndrifted\n", encoding="utf-8")
    assert any("is stale" in p for p in evidence.record_self_consistency_problems(fid))

    evidence._write_markdown(fid)
    assert not [p for p in evidence.record_self_consistency_problems(fid) if "stale" in p]


def test_the_page_check_does_not_recurse(rec):
    """`render_markdown` calls `verify`, which calls this. The first run of the
    check hit unbounded recursion; the guard is what stops it, and a plain
    `verify` call is the shortest proof it holds."""
    fid = rec(method="solo")
    for n in range(1, 5):
        _seed_step(fid, n)
    evidence._write_markdown(fid)
    ok, problems, _ = evidence.verify(fid)
    assert isinstance(problems, list)


@pytest.mark.parametrize(
    ("label", "critic", "row", "flagged"),
    [
        (
            "discloses the wrong layer",
            {"deterministic": {"verdict": "approve"}, "judgment": {"verdict": "block"}},
            "The deterministic layer once returned `block`; all clear now.",
            True,
        ),
        (
            "names the blocked layer",
            {"deterministic": {"verdict": "approve"}, "judgment": {"verdict": "block"}},
            "The judgment critic verdict is `block`.",
            False,
        ),
        (
            "quotes the word for another feature",
            {"deterministic": {"verdict": "approve"}, "judgment": {"verdict": "block"}},
            "SRS-OTHER-001 is `block`; nothing outstanding here.",
            True,
        ),
    ],
)
def test_the_disclosure_must_name_the_layer_that_is_blocked(
    label, critic, row, flagged, rec, tmp_path, monkeypatch
):
    """Accepting any verdict span anywhere on the row was not a disclosure.

    It passed a row that disclosed the DETERMINISTIC layer while hiding a
    judgment `block`, and one that merely quoted the word about a different
    feature. The operator still runs a command that exits 3.
    """
    fid = rec(method="solo")
    r = evidence.load_record(fid)
    r["critic"] = critic
    evidence.save_record(fid, r)
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "verification-queue.md").write_text(
        f"| **{fid}** | 1 | {row} | `close_feature.py {fid} --verified` |\n", encoding="utf-8"
    )
    monkeypatch.setattr(evidence, "ROOT", tmp_path)
    assert bool(evidence._queue_row_problems(fid)) is flagged, label


def test_a_reflowed_queue_row_is_still_inspected(rec, tmp_path, monkeypatch):
    """A markdown row can wrap. The check read LINES containing the command, so
    a row whose disclosure and command sat on different lines escaped it."""
    fid = rec(method="solo")
    r = evidence.load_record(fid)
    r["critic"] = {"deterministic": {"verdict": "approve"}, "judgment": {"verdict": "block"}}
    evidence.save_record(fid, r)
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "verification-queue.md").write_text(
        f"| **{fid}** | 1 | Nothing outstanding at all.\n"
        f"  Really nothing. | `close_feature.py {fid} --verified` |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(evidence, "ROOT", tmp_path)
    assert evidence._queue_row_problems(fid), "a reflowed row must still be checked"
