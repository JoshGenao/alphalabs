"""Browser capture for e2e evidence — screenshots and video, into the record.

An e2e acceptance criterion is a claim about what a human would SEE: "the
dashboard shows IB equity, daily and cumulative P&L, margin usage". A passing
assertion proves a selector matched; it does not let a reviewer look at the page.
For `e2e` and `live-ib` features ``evidence.verify`` therefore requires an image
on the acceptance-criterion step, and this is how a test produces one.

Inside an e2e test body (the example is written without its ``def`` line on
purpose — ``tools/mutation_verify.py`` scans diffs for added ``def test_*`` and a
sample in a docstring reads to it as a real test that never fails, which is a false
finding in the one tool whose worth depends on its findings being trustworthy):

    from tests.e2e.capture import evidence_browser

    with evidence_browser(sync_api, "SRS-UI-003", step=3) as cap:
        page = cap.page(url)
        assert page.locator("#account-equity").is_visible()
        cap.shot(page, "account panel with live equity")

Everything lands in ``.harness/runs/<FID>/artifacts/`` and is attached to the step
on exit, so the artifacts and the record can never disagree about what was run.

Capture is OFF unless ``ATP_CAPTURE_EVIDENCE=1``. A test that only wants to know
whether the page works should not pay for video encoding on every CI run, and a
developer running the suite locally should not silently rewrite the committed
evidence of a feature they are not working on.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import evidence  # noqa: E402  (tools/ is on the path from the line above)

#: Video dimensions. Small on purpose — this is committed to a repo with no
#: git-lfs, and a 1920x1080 recording is two orders of magnitude larger than the
#: evidence value it adds.
VIDEO_SIZE = {"width": 900, "height": 700}


def capture_enabled() -> bool:
    return os.environ.get("ATP_CAPTURE_EVIDENCE") == "1"


class _Capture:
    """Handle passed to the test body. Inert when capture is disabled."""

    def __init__(self, context, fid: str, step: int, tmpdir: Path, enabled: bool):
        self._context = context
        self._fid = fid
        self._step = step
        self._tmp = tmpdir
        self._enabled = enabled
        self._shots: list[tuple[Path, str]] = []
        self._n = 0

    def page(self, url: str | None = None, **goto_kwargs):
        page = self._context.new_page()
        if url:
            page.goto(
                url, wait_until=goto_kwargs.pop("wait_until", "domcontentloaded"), **goto_kwargs
            )
        return page

    def shot(self, page, caption: str, *, element: str | None = None) -> Path | None:
        """Capture the page as it is right now, with what it is meant to show.

        The caption is required, not optional: an unlabelled screenshot in a PR is
        a picture of a dashboard, and the reviewer still has to work out which
        clause of the AC it is supposed to satisfy.

        ``element`` scopes the capture to one CSS selector. Reach for it whenever the
        pane you are evidencing sits far down a long dashboard: a full-page shot of a
        ~4000px page renders that pane a few illegible pixels tall once a reviewer
        views it inline, which is a picture of a dashboard rather than proof of an
        acceptance criterion. Scoped shots stay readable at the size they are actually
        looked at.
        """
        if not self._enabled:
            return None
        self._n += 1
        # A GENERIC filename, with the caption carried in the record instead.
        #
        # Deriving the slug from the caption looked friendlier, but a caption
        # describes the acceptance criterion it evidences — so an artifact for any
        # safety feature ("...hot-swap pane before the swap...") matched
        # SAFETY_PATH_RE by filename and the deterministic critic demanded a
        # tests/domain diff to commit a PNG. The fix is not to loosen that gate, nor
        # to write captions that dodge a regex: it is to stop encoding prose in
        # filenames. `_attach_all` files the caption on the artifact record, which is
        # what EVIDENCE.md renders and what a reviewer actually reads.
        # The trailing token is this CONTEXT's unique tempdir suffix, not prose. Two
        # tests evidencing the same step each restart numbering at 01, and
        # `evidence.attach` keys the stored artifact on the basename — so without it
        # the second test silently overwrites the first one's shots, and the record
        # claims artifacts it no longer holds.
        path = self._tmp / f"{self._n:02d}-{self._tmp.name[-6:]}.png"
        if element is not None:
            target = page.locator(element)
            target.scroll_into_view_if_needed()
            # The dashboard reveals each card with a staggered `rise` animation
            # (styles.css: delay = --i * 90ms + 120ms). Shooting before it finishes
            # captures a transparent element — a perfectly blank "screenshot of the
            # pane" that still gets filed as proof. Wait for it to be opaque.
            # Walk the ANCESTORS, not just the element: the `rise` animation is on
            # the enclosing `.card`, so the target itself reports opacity 1 while its
            # parent is still fully transparent — and the shot comes out blank while
            # every check passes. Effective opacity is the product down the chain.
            page.wait_for_function(
                "sel => { let el = document.querySelector(sel); if (!el) return false;"
                " while (el) {"
                "   if (parseFloat(getComputedStyle(el).opacity) < 0.99) return false;"
                "   el = el.parentElement; }"
                " return true; }",
                arg=element,
                timeout=10_000,
            )
            target.screenshot(path=str(path))
            self._shots.append((path, caption))
            return path
        page.screenshot(path=str(path), full_page=True)
        self._shots.append((path, caption))
        return path


@contextlib.contextmanager
def evidence_browser(sync_api, fid: str, step: int, *, video: bool = True, **launch_kwargs):
    """A chromium context whose screenshots and video become the feature's evidence.

    Attachment happens on the way OUT, after ``context.close()`` — Playwright
    finalises a video only when its context closes, so attaching earlier would file
    a truncated or zero-byte file as proof. Failures to attach are reported, never
    swallowed: an artifact silently missing from a record that claims one is worse
    than no artifact at all (CLAUDE.md rule 3).
    """
    enabled = capture_enabled()
    tmpdir = Path(tempfile.mkdtemp(prefix=f"atp-capture-{fid}-"))
    video_dir = tmpdir / "video"
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        ctx_kwargs = {}
        if enabled and video:
            ctx_kwargs["record_video_dir"] = str(video_dir)
            ctx_kwargs["record_video_size"] = VIDEO_SIZE
            ctx_kwargs["viewport"] = VIDEO_SIZE
        context = browser.new_context(**ctx_kwargs)
        cap = _Capture(context, fid, step, tmpdir, enabled)
        try:
            yield cap
        finally:
            context.close()  # finalises the video file
            browser.close()
            if enabled:
                _attach_all(cap, fid, step, video_dir)
            shutil.rmtree(tmpdir, ignore_errors=True)


def _attach_all(cap: _Capture, fid: str, step: int, video_dir: Path) -> None:
    problems = []
    for path, caption in cap._shots:
        try:
            evidence.attach(fid, step, path, caption)
        except evidence.EvidenceError as exc:
            problems.append(f"{path.name}: {exc}")
    if video_dir.is_dir():
        for vid in sorted(video_dir.glob("*.webm")):
            try:
                evidence.attach(fid, step, vid, "full session recording")
            except evidence.EvidenceError as exc:
                problems.append(f"{vid.name}: {exc}")
    if problems:
        # Print rather than raise: the test's own verdict is about the feature, and
        # turning a passing feature test red because a screenshot was 200 KB over
        # the cap would put the artifact pipeline in charge of the test result.
        # `evidence.verify` is what refuses the CLOSE, and it will still refuse it.
        print(f"\n⚠ {fid} step {step}: {len(problems)} artifact(s) NOT attached:")
        for p in problems:
            print(f"    · {p}")
