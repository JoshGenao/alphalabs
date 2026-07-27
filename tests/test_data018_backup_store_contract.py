"""L3 contract test: the SRS-DATA-018 backup module's copies of the backtest-store literals must
agree with the definitions that own them.

``crates/atp-data/src/backup.rs`` has to recognise ``atp-simulation``'s ``backtest_results.store``
blobs so SYS-59's "backup all NAS data (... backtest results)" is actually covered — but it must not
*import* them: ``atp-data`` is a lower layer and ``atp-simulation`` already depends on it, so that
edge would invert the architecture and create a dependency cycle.

The literals are therefore duplicated on purpose, which makes them a drift hazard: rename the
constant in ``atp-simulation`` and the backup would silently stop discovering backtest results,
reporting a clean "verified" run over an archive that is quietly missing a whole data family. That
is the worst failure mode a backup can have — a false green.

This test is the tripwire. It fails loudly the moment the two sides disagree.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP_RS = ROOT / "crates" / "atp-data" / "src" / "backup.rs"
BACKTEST_STORE_RS = ROOT / "crates" / "atp-simulation" / "src" / "backtest_store.rs"


def _const(source: Path, name: str) -> str:
    """Extract a ``pub const <name>: &str = "...";`` value from a Rust source file."""
    text = source.read_text(encoding="utf-8")
    match = re.search(
        rf'^pub const {re.escape(name)}\s*:\s*&str\s*=\s*"([^"]*)"\s*;',
        text,
        re.MULTILINE,
    )
    if match is None:
        raise AssertionError(
            f"could not find `pub const {name}: &str` in {source.relative_to(ROOT)} — "
            f"the SRS-DATA-018 backup drift check cannot verify what it cannot locate"
        )
    return match.group(1)


class Data018BackupStoreContractTest(unittest.TestCase):
    def test_source_files_exist(self) -> None:
        self.assertTrue(BACKUP_RS.is_file(), f"missing {BACKUP_RS}")
        self.assertTrue(BACKTEST_STORE_RS.is_file(), f"missing {BACKTEST_STORE_RS}")

    def test_backtest_store_filename_matches_its_owning_definition(self) -> None:
        owner = _const(BACKTEST_STORE_RS, "STORE_FILENAME")
        copy = _const(BACKUP_RS, "BACKTEST_STORE_FILENAME")
        self.assertEqual(
            copy,
            owner,
            "atp-data::backup::BACKTEST_STORE_FILENAME has drifted from "
            "atp-simulation::backtest_store::STORE_FILENAME — the backup would silently stop "
            "discovering backtest-results units and report a false-verified archive",
        )

    def test_backtest_store_magic_matches_its_owning_definition(self) -> None:
        owner = _const(BACKTEST_STORE_RS, "MAGIC")
        copy = _const(BACKUP_RS, "BACKTEST_STORE_MAGIC")
        self.assertEqual(
            copy,
            owner,
            "atp-data::backup::BACKTEST_STORE_MAGIC has drifted from "
            "atp-simulation::backtest_store::MAGIC — envelope verification would reject every "
            "backtest-results unit as corrupt",
        )

    def test_the_two_store_families_remain_distinguishable(self) -> None:
        """Market-data and backtest-results blobs must not share a filename or a magic header, or
        the backup could not tell which codec owns a discovered unit."""
        market_data_store_rs = ROOT / "crates" / "atp-data" / "src" / "store.rs"
        self.assertNotEqual(
            _const(market_data_store_rs, "STORE_FILENAME"),
            _const(BACKTEST_STORE_RS, "STORE_FILENAME"),
        )
        self.assertNotEqual(
            _const(market_data_store_rs, "MAGIC"),
            _const(BACKTEST_STORE_RS, "MAGIC"),
        )

    def test_atp_data_does_not_depend_on_atp_simulation(self) -> None:
        """The reason the literals are duplicated at all. If this edge ever appears, replace the
        copies with a real import and delete this test."""
        manifest = (ROOT / "crates" / "atp-data" / "Cargo.toml").read_text(encoding="utf-8")
        self.assertNotIn(
            "atp-simulation",
            manifest,
            "atp-data now depends on atp-simulation: import the backtest-store constants "
            "directly instead of keeping duplicated literals",
        )


if __name__ == "__main__":
    unittest.main()
