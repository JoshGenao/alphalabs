"""L1 unit — the SRS-RES-003 navigation control's client-side contract.

The browser is where the acceptance criterion is finally kept or broken, so the
two properties that decide it are pinned against the SHIPPED ``app.js`` source
rather than a copy:

* **The same-origin guard agrees with the server's.** ``isSameOriginPath`` is
  the last check before a path becomes an iframe ``src``; if it drifts from
  :func:`atp_dashboard.navigation.same_origin_target`, defence in depth becomes
  a second, weaker opinion. When ``node`` is available the shipped predicate is
  EXECUTED over the same adversarial corpus as the Python allow-list and the
  two verdicts must match exactly.
* **Nothing is left armed on stale or malformed truth.** The control arms only
  on ``routable === true`` plus a fresh reachable probe, and every degraded
  branch clears the target a click would otherwise find.

Static assertions carry the invariants that must hold even where ``node`` is
absent (the repo's existing convention for ``app.js``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from atp_dashboard.navigation import same_origin_target

_APP_JS = (Path(__file__).resolve().parents[2] / "python/atp_dashboard/assets/app.js").read_text(
    encoding="utf-8"
)

#: The corpus the Python allow-list is held to — the JS mirror must agree on
#: every entry, especially each spelling of a direct service URL.
_TARGET_CORPUS = (
    "/research/",
    "/research/lab",
    "/dashboard/api/research",
    "/a",
    "http://127.0.0.1:8888/research/",
    "https://jupyter.internal/research/",
    "//jupyter.internal/research/",
    "/\\jupyter.internal/research/",
    "javascript:alert(1)",
    "data:text/html,<script>1</script>",
    "/research/:8888",
    "/user:pass@host/research/",
    "/research/../../etc/passwd",
    "research/",
    "",
    "/research/\n",
    "/research/ ",
    " /research/",
    "/research/\x7f",
)


def _extract(name: str) -> str:
    """Return the source of a top-level ``function <name>(...) {...}`` in app.js."""

    marker = f"function {name}("
    start = _APP_JS.index(marker)
    brace = _APP_JS.index("{", start)
    depth = 0
    for index in range(brace, len(_APP_JS)):
        if _APP_JS[index] == "{":
            depth += 1
        elif _APP_JS[index] == "}":
            depth -= 1
            if depth == 0:
                return _APP_JS[start : index + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def _run_node(script: str) -> object:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - environment dependent
        pytest.skip("node is not available to execute the shipped predicate")
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# --------------------------------------------------------------------------- #
# The guard the browser actually runs
# --------------------------------------------------------------------------- #


def test_client_same_origin_guard_matches_the_server_allow_list() -> None:
    """Executed, not merely read: both mirrors must refuse the same inputs."""

    script = (
        _extract("isSameOriginPath")
        + "\nconst corpus = "
        + json.dumps(list(_TARGET_CORPUS))
        + ";\nconsole.log(JSON.stringify(corpus.map(isSameOriginPath)));"
    )
    verdicts = _run_node(script)
    expected = [same_origin_target(target) is not None for target in _TARGET_CORPUS]
    assert verdicts == expected


def test_client_guard_refuses_non_strings() -> None:
    script = (
        _extract("isSameOriginPath")
        + "\nconst corpus = [null, undefined, 1, {}, [], ['/research/']];"
        + "\nconsole.log(JSON.stringify(corpus.map(isSameOriginPath)));"
    )
    assert _run_node(script) == [False] * 6


# --------------------------------------------------------------------------- #
# Fail-closed projection of the navigation model
# --------------------------------------------------------------------------- #


def test_malformed_navigation_feeds_never_yield_a_routable_entry() -> None:
    """A malformed feed must disarm, not throw its way past the disarm."""

    feeds = [
        None,
        "not-json-object",
        [],
        {},
        {"entries": None},
        {"entries": "research"},
        {"entries": [None]},
        {"entries": [{"id": "other", "routable": True, "target": "/research/"}]},
        # Truthy-but-not-true routability must not arm the control.
        {"entries": [{"id": "research", "routable": "true", "target": "/research/"}]},
        {"entries": [{"id": "research", "routable": 1, "target": "/research/"}]},
        # Routable, but the target is a direct service URL -> refused here too.
        {
            "entries": [
                {
                    "id": "research",
                    "routable": True,
                    "target": "http://127.0.0.1:8888/research/",
                }
            ]
        },
        {"entries": [{"id": "research", "routable": True, "target": None}]},
    ]
    script = (
        _extract("isSameOriginPath")
        + "\n"
        + _extract("plainObject")
        + "\n"
        + _extract("researchNavEntry")
        + "\nconst feeds = "
        + json.dumps(feeds)
        + ";\nconsole.log(JSON.stringify(feeds.map((f) => {"
        + "  const e = researchNavEntry(f);"
        + "  return e === null ? null : {routable: e.routable, target: e.target};"
        + "})));"
    )
    for feed, projected in zip(feeds, _run_node(script), strict=True):
        if projected is not None:
            assert projected["routable"] is False, feed
            assert projected["target"] is None, feed


def test_a_well_formed_routable_feed_projects_the_same_origin_target() -> None:
    script = (
        _extract("isSameOriginPath")
        + "\n"
        + _extract("plainObject")
        + "\n"
        + _extract("researchNavEntry")
        + "\nconsole.log(JSON.stringify(researchNavEntry("
        + '{"entries": [{"id": "research", "routable": true, "target": "/research/",'
        + ' "detail": "ok"}]})));'
    )
    assert _run_node(script) == {
        "routable": True,
        "target": "/research/",
        "detail": "ok",
    }


# --------------------------------------------------------------------------- #
# Static invariants (hold with or without node)
# --------------------------------------------------------------------------- #


def test_the_embed_is_opened_through_exactly_one_guarded_helper() -> None:
    """One funnel, so a new caller cannot bypass the same-origin check."""

    assert _APP_JS.count("frame.src = path;") == 1
    assert "function openResearchEmbed(path) {" in _APP_JS
    assert "if (!frame || !isSameOriginPath(path)) return false;" in _APP_JS


def test_the_control_arms_only_on_a_fresh_reachable_probe() -> None:
    assert 'state = "ready";' in _APP_JS
    # Both gates precede the armed branch, and staleness is bounded.
    assert "!researchLiveFresh()" in _APP_JS
    assert "!researchLive.reachable" in _APP_JS
    assert "const RESEARCH_LIVE_STALE_MS = POLL_MS * 3;" in _APP_JS
    # Every non-armed path clears the target a click would find.
    assert "delete link.dataset.embedPath;" in _APP_JS


def test_every_degraded_navigation_branch_disarms() -> None:
    """404, HTTP error, malformed body and a stalled fetch all clear the fact."""

    poll = _APP_JS[_APP_JS.index("async function pollNavigation()") :]
    poll = poll[: poll.index("\n  }\n") + 4]
    assert poll.count("navEntry = null;") == 3  # 404, non-ok, exception
    assert "signal: AbortSignal.timeout(POLL_MS)" in poll
    # A stalled RESEARCH probe must disarm too, not linger as stale truth.
    research_poll = _APP_JS[_APP_JS.index("async function pollResearch()") :]
    research_poll = research_poll[: research_poll.index("\n  }\n") + 4]
    assert research_poll.count("setResearchLive(null);") == 3
    assert "signal: AbortSignal.timeout(POLL_MS)" in research_poll


def test_navigation_never_targets_a_control_route() -> None:
    """The navigation control is a read path; it POSTs nothing, ever."""

    start = _APP_JS.index("// ----- SRS-RES-003 primary research navigation")
    end = _APP_JS.index("async function pollAlerts()", start)
    nav_block = _APP_JS[start:end]
    assert 'method: "POST"' not in nav_block
    assert "/api/v1/" not in nav_block
    assert "fetch(NAVIGATION_ROUTE" in nav_block  # the only request it makes
    assert nav_block.count("fetch(") == 1
