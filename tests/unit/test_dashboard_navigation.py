"""L1 unit — SRS-RES-003 primary-navigation model (SyRS SYS-43).

The acceptance criterion is *"the operator can open the embedded Jupyter
environment from the primary dashboard workflow **without using a direct
service URL**"*. Two properties carry it, and both are asserted here:

* the navigation target is structurally same-origin — the fail-closed
  allow-list refuses every spelling of an absolute/service URL, and a rejected
  target is not echoed back (echoing would republish the very URL the criterion
  forbids);
* an entry that cannot route is an explicit deferred cell naming its knob and
  owning feature — never a dead link that merely looks actionable.

The model is also PROBE-FREE: routability is a composition fact, so serving it
costs no upstream traffic and cannot masquerade as liveness (that stays the
SRS-RES-001 probe's answer).
"""

from __future__ import annotations

import json

from atp_dashboard.navigation import (
    NAVIGATION_SRS_REF,
    RESEARCH_ENTRY_ID,
    RESEARCH_ENTRY_LABEL,
    RESEARCH_PANEL_ANCHOR,
    PrimaryNavigationProvider,
    same_origin_target,
)
from atp_dashboard.research import (
    RESEARCH_PREFIX,
    UPSTREAM_ENV_KNOB,
    ResearchEnvironmentProvider,
)

_STATE_ROUTE = "/dashboard/api/research"


def _entry(provider: PrimaryNavigationProvider) -> dict[str, object]:
    snapshot = provider.navigation_snapshot()
    assert snapshot["srs_ref"] == NAVIGATION_SRS_REF
    assert isinstance(snapshot["generated_at"], str)
    entries = snapshot["entries"]
    assert isinstance(entries, list) and len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, dict)
    return entry


# --------------------------------------------------------------------------- #
# same_origin_target — the "no direct service URL" allow-list
# --------------------------------------------------------------------------- #


def test_root_relative_same_origin_paths_are_accepted() -> None:
    for target in ("/research/", "/research/lab", "/dashboard/api/research", "/a"):
        assert same_origin_target(target) == target


def test_every_service_url_spelling_is_refused() -> None:
    """No scheme, no host, no userinfo, no protocol-relative, no traversal."""

    for target in (
        "http://127.0.0.1:8888/research/",  # the direct service URL itself
        "https://jupyter.internal/research/",
        "//jupyter.internal/research/",  # protocol-relative -> other origin
        "/\\jupyter.internal/research/",  # backslash spelling browsers fold
        "\\\\jupyter.internal\\research",
        "javascript:alert(1)",  # scheme with no slash at all
        "/research/:8888",  # any ':' is refused outright
        "/user:pass@host/research/",
        "/research/../../etc/passwd",  # traversal out of the prefix
        "research/",  # relative -> resolves off the page
        "",
    ):
        assert same_origin_target(target) is None, target


def test_non_strings_and_control_characters_are_refused() -> None:
    for target in (None, 1, b"/research/", ["/research/"], {"t": "/research/"}):
        assert same_origin_target(target) is None
    for target in ("/research/\n", "/research/ ", " /research/", "/research/\x7f"):
        assert same_origin_target(target) is None


# --------------------------------------------------------------------------- #
# The navigation entry
# --------------------------------------------------------------------------- #


def test_routable_entry_targets_the_same_origin_prefix() -> None:
    entry = _entry(
        PrimaryNavigationProvider(research_routable=True, research_state_route=_STATE_ROUTE)
    )
    assert entry["id"] == RESEARCH_ENTRY_ID
    assert entry["label"] == RESEARCH_ENTRY_LABEL
    assert entry["panel_anchor"] == RESEARCH_PANEL_ANCHOR
    assert entry["routable"] is True
    assert entry["target"] == RESEARCH_PREFIX
    assert same_origin_target(entry["target"]) == RESEARCH_PREFIX
    # Liveness is deliberately NOT this model's to answer.
    assert entry["state_route"] == _STATE_ROUTE


def test_unroutable_entry_is_an_explicit_deferred_cell_with_no_target() -> None:
    entry = _entry(
        PrimaryNavigationProvider(research_routable=False, research_state_route=_STATE_ROUTE)
    )
    assert entry["routable"] is False
    assert entry["target"] is None
    detail = entry["detail"]
    assert isinstance(detail, str)
    assert UPSTREAM_ENV_KNOB in detail  # names the knob
    assert "SRS-RES-001" in detail  # names the owning feature


def test_a_non_same_origin_prefix_fails_closed_without_echoing_it() -> None:
    """A mis-composed provider must not turn into a direct-service-URL link.

    Nor may the refusal quote the rejected value: the detail string is served
    to the browser, so echoing it would publish exactly the URL SYS-43 forbids.
    """

    upstream = "http://127.0.0.1:8888"
    entry = _entry(
        PrimaryNavigationProvider(
            research_routable=True,
            research_state_route=_STATE_ROUTE,
            research_prefix=upstream,
        )
    )
    assert entry["routable"] is False
    assert entry["target"] is None
    assert upstream not in json.dumps(entry)
    assert "127.0.0.1" not in json.dumps(entry)


def test_a_non_same_origin_state_route_also_fails_closed() -> None:
    entry = _entry(
        PrimaryNavigationProvider(
            research_routable=True,
            research_state_route="http://127.0.0.1:8888/dashboard/api/research",
        )
    )
    assert entry["routable"] is False
    assert entry["state_route"] is None
    assert "8888" not in json.dumps(entry)


# --------------------------------------------------------------------------- #
# for_research — routability derives from the SAME fact that mounts the proxy
# --------------------------------------------------------------------------- #


def test_for_research_derives_routability_without_serving_the_upstream() -> None:
    upstream = "http://127.0.0.1:8888"
    provider = PrimaryNavigationProvider.for_research(
        ResearchEnvironmentProvider(upstream), state_route=_STATE_ROUTE
    )
    snapshot = provider.navigation_snapshot()
    assert snapshot["entries"][0]["routable"] is True
    # The upstream is the one thing the operator must never need — and never
    # sees. It appears nowhere in the served model.
    body = json.dumps(snapshot)
    assert upstream not in body
    assert "127.0.0.1" not in body
    assert "8888" not in body


def test_for_research_without_an_upstream_is_not_routable() -> None:
    provider = PrimaryNavigationProvider.for_research(
        ResearchEnvironmentProvider(None), state_route=_STATE_ROUTE
    )
    assert provider.navigation_snapshot()["entries"][0]["routable"] is False


def test_empty_upstream_reads_as_not_routable() -> None:
    """`ResearchEnvironmentProvider` normalises "" to None — so must the nav."""

    provider = PrimaryNavigationProvider.for_research(
        ResearchEnvironmentProvider(""), state_route=_STATE_ROUTE
    )
    assert provider.navigation_snapshot()["entries"][0]["routable"] is False


def test_snapshot_is_probe_free_and_json_serialisable() -> None:
    """No I/O: an unreachable (indeed nonexistent) upstream still answers fast.

    Composition is not liveness — the model must not probe, so it cannot block
    or fabricate a reachability claim.
    """

    provider = PrimaryNavigationProvider.for_research(
        # Port 1 is not listening; a probing implementation would stall here.
        ResearchEnvironmentProvider("http://127.0.0.1:1"),
        state_route=_STATE_ROUTE,
    )
    snapshot = provider.navigation_snapshot()
    assert snapshot["entries"][0]["routable"] is True  # registered != reachable
    assert json.loads(json.dumps(snapshot)) == snapshot


def test_the_model_carries_no_control_affordance() -> None:
    """Navigation describes destinations, never actions."""

    body = json.dumps(
        PrimaryNavigationProvider(
            research_routable=True, research_state_route=_STATE_ROUTE
        ).navigation_snapshot()
    ).lower()
    for token in ("post", "method", "action", "confirm", "activate", "submit"):
        assert token not in body
