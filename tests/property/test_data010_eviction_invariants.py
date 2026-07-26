"""L2 property tests for SRS-DATA-010 — the eviction CLI's fail-closed input boundary.

The ``data010_eviction_cli`` is the operator's DESTRUCTIVE surface, so its parsing of the protection
inputs and the access journal must be robust across a large generated input space: a **well-formed**
protection file / journal must always be accepted, and a **malformed** one must always be rejected
fail-closed (never silently ignored, which could drop a never-evict directive and let live data be
evicted). Hypothesis generates both classes and asserts the boundary holds.

(The planner's ordering / never-evict-pinned invariants are exercised by the Rust inline sweep
``no_pinned_symbol_ever_appears_in_plan_invariant`` and the L4 CLI integration test; this file covers
the input-robustness invariant that is cheapest and most valuable to fuzz from Python.)
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[2]
EVICT_CLI = ROOT / "target" / "debug" / "data010_eviction_cli"

_SYMBOL = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=5)
_TS = st.integers(min_value=0, max_value=2_000_000_000)
_JOB_KIND = st.sampled_from(["backtest", "factor-pipeline"])
_JOB_ID = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=6)


def _build_cli() -> None:
    if EVICT_CLI.exists() or shutil.which("cargo") is None:
        return
    subprocess.run(
        ["cargo", "build", "-p", "atp-data", "--bin", "data010_eviction_cli"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


@st.composite
def _well_formed_protection_file(draw) -> str:
    lines = []
    for _ in range(draw(st.integers(min_value=0, max_value=8))):
        kind = draw(st.sampled_from(["live", "active", "watchlist", "access"]))
        sym = draw(_SYMBOL)
        if kind == "access":
            lines.append(f"{kind} {sym} {draw(_TS)}")
        else:
            lines.append(f"{kind} {sym}")
    return "\n".join(lines) + "\n"


@st.composite
def _well_formed_journal(draw) -> str:
    lines = []
    for _ in range(draw(st.integers(min_value=0, max_value=8))):
        lines.append(f"{draw(_TS)}\t{draw(_JOB_KIND)}\t{draw(_JOB_ID)}\t{draw(_SYMBOL)}")
    return "\n".join(lines) + "\n"


class Data010InputBoundaryProperty(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _build_cli()
        if not EVICT_CLI.exists():
            raise unittest.SkipTest("data010_eviction_cli not built (cargo unavailable)")
        cls._base = Path(tempfile.mkdtemp(prefix="atp-data010-prop-"))
        (cls._base / "ssd" / "access_journal").mkdir(parents=True)
        (cls._base / "nas").mkdir()
        cls._ssd = cls._base / "ssd"
        cls._nas = cls._base / "nas"

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._base, ignore_errors=True)

    def _report(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                str(EVICT_CLI),
                "report",
                "--ssd",
                str(self._ssd),
                "--nas",
                str(self._nas),
                "--ssd-capacity",
                "100",
                *extra,
            ],
            capture_output=True,
            text=True,
        )

    @settings(
        max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(body=_well_formed_protection_file())
    def test_well_formed_protection_file_is_always_accepted(self, body: str) -> None:
        prot = self._base / "prot.txt"
        prot.write_text(body)
        result = self._report("--protection-inputs", str(prot))
        self.assertEqual(
            result.returncode, 0, f"well-formed file rejected:\n{body}\n{result.stderr}"
        )

    @settings(
        max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(directive=st.text(alphabet="abcdefghij", min_size=1, max_size=8), sym=_SYMBOL)
    def test_unknown_directive_is_always_rejected(self, directive: str, sym: str) -> None:
        # An unknown directive keyword must fail closed (never silently skipped).
        assume(directive not in {"live", "active", "watchlist", "access"})
        prot = self._base / "prot.txt"
        prot.write_text(f"live {sym}\n{directive} {sym}\n")
        result = self._report("--protection-inputs", str(prot))
        self.assertNotEqual(result.returncode, 0, f"unknown directive '{directive}' was accepted")

    @settings(
        max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(sym=_SYMBOL, bad_ts=st.text(alphabet="xyz.-", min_size=1, max_size=5))
    def test_non_integer_access_timestamp_is_always_rejected(self, sym: str, bad_ts: str) -> None:
        prot = self._base / "prot.txt"
        prot.write_text(f"access {sym} {bad_ts}\n")
        result = self._report("--protection-inputs", str(prot))
        self.assertNotEqual(result.returncode, 0, f"non-integer access ts '{bad_ts}' was accepted")

    @settings(
        max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(body=_well_formed_journal())
    def test_well_formed_journal_is_always_read_cleanly(self, body: str) -> None:
        (self._ssd / "access_journal" / "access_journal.log").write_text(body)
        result = self._report("--use-journal")
        self.assertEqual(
            result.returncode, 0, f"well-formed journal rejected:\n{body}\n{result.stderr}"
        )

    @settings(
        max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(kind=_JOB_KIND, jid=_JOB_ID, sym=_SYMBOL)
    def test_corrupt_journal_line_always_fails_closed(self, kind: str, jid: str, sym: str) -> None:
        # A complete (newline-terminated) line with a non-integer timestamp is corruption → fail closed.
        (self._ssd / "access_journal" / "access_journal.log").write_text(
            f"NOT_A_TS\t{kind}\t{jid}\t{sym}\n"
        )
        result = self._report("--use-journal")
        self.assertNotEqual(result.returncode, 0, "a corrupt journal line must fail closed")


if __name__ == "__main__":
    unittest.main()
