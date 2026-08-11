"""SRS-RESV-006 / SyRS SYS-49e — invariants of the cool-down leg over generated CLI output.

L2 property. The example-based suites pick the proof streams a correct binary emits; this
one generates streams a *wrong* one might, and asserts the two things that must hold for
every input:

1. **UNKNOWN never renders as "no cool-down".** Not for a missing line, a misspelled key, a
   contradictory pair, or a truncated stream. That reading is a false all-clear about
   whether an automatic live-strategy swap may fire, and the pane's promote control keys
   off it (``CLAUDE.md`` rule 3).
2. **``in_effect: True`` never travels without both window boundaries.** A half-known
   window would draw a countdown dial over an expiry nobody reported.

Anything the reader cannot vouch for must raise :class:`HotSwapStatusUnavailable` — which
the pane renders as an explicit unreadable state — rather than return a value.
"""

from __future__ import annotations

import subprocess

import pytest
from atp_hotswap import CliHotSwapCooldownSource, HotSwapStatusUnavailable
from hypothesis import given
from hypothesis import strategies as st

pytestmark = pytest.mark.property


def _source(stdout: str, returncode: int = 0) -> CliHotSwapCooldownSource:
    def runner(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    return CliHotSwapCooldownSource("/x/cooldown.json", binary="/bin/true", runner=runner)


def _read(stdout: str, returncode: int = 0) -> dict | None:
    """The leg's answer, or ``None`` when it refused. Never raises."""

    try:
        return _source(stdout, returncode).live_state()
    except HotSwapStatusUnavailable:
        return None


#: Every key the leg reads, plus near-misses a version-skewed binary might emit.
_KEYS = st.sampled_from(
    [
        "cooldown-state",
        "cooldown-in-effect",
        "cooldown-started-at-seconds",
        "cooldown-expires-at-seconds",
        "cooldown-days-default",
        "cooldown-unreadable-reason",
        "cooldown_in_effect",  # underscore instead of hyphen
        "cooldown-in-effect ",  # trailing space
        "observed-at-seconds",
        "unrelated-key",
    ]
)
_VALUES = st.sampled_from(
    ["true", "false", "True", "", "UNKNOWN", "ACTIVE", "EXPIRED", "NEVER_SWAPPED", "0", "-1", "x"]
)


@given(lines=st.lists(st.tuples(_KEYS, _VALUES), max_size=8), returncode=st.integers(0, 1))
def test_an_arbitrary_proof_stream_never_fabricates_a_clear_window(lines, returncode) -> None:
    stdout = "".join(f"{key}:{value}\n" for key, value in lines)
    state = _read(stdout, returncode)
    if state is None:
        return  # refused, which is always a safe answer

    cooldown = state["cooldown"]
    # Reaching here means the leg VOUCHED for the window, so the binary must have said
    # exactly one of the three answer states — never UNKNOWN, and never nothing at all.
    parsed = dict(
        line.split(":", 1)
        for line in stdout.splitlines()
        if ":" in line  # noqa: B905
    )
    assert parsed.get("cooldown-state") in ("ACTIVE", "EXPIRED", "NEVER_SWAPPED")
    assert isinstance(cooldown["in_effect"], bool), cooldown
    if parsed["cooldown-state"] == "UNKNOWN":  # pragma: no cover - guarded above
        pytest.fail("an UNKNOWN window was rendered as an answer")


@given(lines=st.lists(st.tuples(_KEYS, _VALUES), max_size=8))
def test_an_in_effect_window_always_carries_both_boundaries(lines) -> None:
    stdout = "".join(f"{key}:{value}\n" for key, value in lines)
    state = _read(stdout)
    if state is None:
        return

    cooldown = state["cooldown"]
    if cooldown["in_effect"] is True:
        assert "started_at" in cooldown and "expires_at" in cooldown, (
            f"an in-effect window must report both boundaries: {cooldown}"
        )
        assert cooldown["started_at"] and cooldown["expires_at"]


@given(returncode=st.integers(min_value=1, max_value=3))
def test_a_nonzero_exit_is_never_an_answer(returncode) -> None:
    # The binary exits non-zero ONLY on an unreadable window (an ACTIVE one is healthy and
    # exits zero), so a non-zero exit must never produce a rendered cool-down — even when
    # the stdout looks perfectly well-formed.
    healthy_looking = (
        "cooldown-state:NEVER_SWAPPED\ncooldown-in-effect:false\ncooldown-days-default:7\n"
    )
    assert _read(healthy_looking, returncode) is None


@given(text=st.text(max_size=200))
def test_arbitrary_junk_is_refused_rather_than_crashing(text) -> None:
    # The leg is reading another process's stdout; a wedged or wrong binary can emit
    # anything. Every outcome must be either a vouched-for window or an explicit refusal —
    # never an unhandled exception, which the pane would render as a 500 rather than as its
    # honest unreadable state.
    state = _read(text)
    if state is not None:
        assert isinstance(state["cooldown"]["in_effect"], bool)
