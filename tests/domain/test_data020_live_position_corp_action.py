"""SRS-DATA-020 — adjust live positions affected by corporate actions.

L7 domain (safety) scenario test. Anchors the safety post-conditions of the
live-position corporate-action planner in the domain-test layer and walks the AC
end-to-end over fixtures:

* The Rust suites ``srs_data_020_position_corp_action`` (the split / dividend /
  merger / symbol-change / delisting transform math over signed positions) and
  ``srs_data_020_corp_action_notify`` (the operator-notification clause: every
  delisting and every review — and only those — is emitted through the neutral alert
  port carrying the symbol + reason) pass.
* The ``data020_position_corp_action_cli`` scenario CLI adjusts a position's quantity
  and cost basis for a split, reduces the basis additively for a cash dividend, remaps
  a position to its successor on a merger / symbol change, marks a position DELISTED
  and emits BOTH the operator-notification intent and the strategy-callback intent,
  sends an un-applicable action to MANUAL_REVIEW with a notification intent, flags a
  successor collision, and fails closed (exit 2) on bad input.

The three AC clauses proven here over fixtures:
  1. quantities + average cost basis are adjusted for splits / reverse splits /
     dividends;
  2. mergers and symbol changes remap positions to successor securities;
  3. delistings mark positions delisted and notify the operator.

Completeness: **serialized** — this proves the deterministic core over fixtures. The
live position feed **carrying cost basis** (the brokerage adapter positions sync,
SRS-EXE-006 / API-5), real operator email/SMS (SRS-NOTIF-001), and the live
position-change strategy callback (SRS-SDK-004 — the SDK's ``deliver_order_event``
seam is order-event-specific; a position-change callback surface is deferred to that
owner) are the end-to-end integration that keeps SRS-DATA-020 ``passes:false``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.domain, pytest.mark.safety]

CLI_BIN = "data020_position_corp_action_cli"
PACKAGE = "atp-execution"


# --------------------------------------------------------------------------- #
# Rust suites (shelled so the safety post-conditions are anchored here)
# --------------------------------------------------------------------------- #


def _cargo() -> str:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run the Rust DATA-020 suites")
    return cargo


def _run_cargo_test(suite: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_cargo(), "test", "-p", PACKAGE, "--test", suite],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_all_passed(result: subprocess.CompletedProcess[str], label: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{label} Rust suite failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "test result: ok." in combined and "0 failed" in combined, (
        f"unexpected cargo test output for {label}:\n{combined}"
    )


def test_rust_transform_suite_passes() -> None:
    _assert_all_passed(_run_cargo_test("srs_data_020_position_corp_action"), "transform")


def test_rust_notification_emission_suite_passes() -> None:
    _assert_all_passed(_run_cargo_test("srs_data_020_corp_action_notify"), "notification-emission")


# --------------------------------------------------------------------------- #
# Scenario CLI (fixture positions + file reads + persisted-output inspection)
# --------------------------------------------------------------------------- #


def _cli_path() -> Path:
    cargo = _cargo()
    build = subprocess.run(
        [cargo, "build", "-p", PACKAGE, "--bin", CLI_BIN],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"cli build failed:\n{build.stderr}"
    path = ROOT / "target" / "debug" / CLI_BIN
    assert path.exists(), f"built CLI not found at {path}"
    return path


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_cli_path()), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _outcomes(stdout: str) -> list[dict]:
    return [
        json.loads(line[len("outcome:") :])
        for line in stdout.splitlines()
        if line.startswith("outcome:")
    ]


def test_cli_split_adjusts_quantity_and_keeps_basis_invariant() -> None:
    # AC clause 1: quantity + average cost basis adjusted for a split.
    result = _run_cli(
        [
            "plan",
            "--symbol",
            "AAPL",
            "--split",
            "4:1",
            "--position",
            "sym=AAPL,qty=100,basis=500000",
        ]
    )
    assert result.returncode == 0, result.stderr
    (pos,) = _outcomes(result.stdout)
    assert pos["result"] == "ADJUSTED"
    assert pos["before"]["quantity"] == 100 and pos["after"]["quantity"] == 400
    assert pos["after"]["cost_basis_minor"] == 500000, "basis invariant under a split"
    assert pos["after"]["avg_cost_minor"] == 1250
    # The strategy is told its position changed.
    assert pos["callback"]["kind"] == "ADJUSTED"


def test_cli_dividend_reduces_basis_additively() -> None:
    # AC clause 1: average cost basis adjusted for a cash dividend (500000 - 100*100).
    result = _run_cli(
        [
            "plan",
            "--symbol",
            "AAPL",
            "--dividend",
            "100:4000",
            "--position",
            "sym=AAPL,qty=100,basis=500000",
        ]
    )
    assert result.returncode == 0, result.stderr
    (pos,) = _outcomes(result.stdout)
    assert pos["result"] == "ADJUSTED"
    assert pos["after"]["quantity"] == 100, "a cash dividend never changes the share count"
    assert pos["after"]["cost_basis_minor"] == 490000


def test_cli_merger_remaps_to_successor() -> None:
    # AC clause 2: a merger remaps the position to its successor security.
    result = _run_cli(
        [
            "plan",
            "--symbol",
            "OLD",
            "--merger",
            "NEW:3:2:0",
            "--position",
            "sym=OLD,qty=200,basis=800000",
        ]
    )
    assert result.returncode == 0, result.stderr
    (pos,) = _outcomes(result.stdout)
    assert pos["result"] == "REMAPPED"
    assert pos["successor"] == "NEW"
    assert pos["after"]["symbol"] == "NEW"
    assert pos["after"]["quantity"] == 300
    assert pos["after"]["cost_basis_minor"] == 800000, "basis carries intact"


def test_cli_symbol_change_relabels() -> None:
    # AC clause 2: a symbol change remaps the position to its successor.
    result = _run_cli(
        [
            "plan",
            "--symbol",
            "OLD",
            "--symbol-change",
            "NEW",
            "--position",
            "sym=OLD,qty=100,basis=500000",
        ]
    )
    assert result.returncode == 0, result.stderr
    (pos,) = _outcomes(result.stdout)
    assert pos["result"] == "REMAPPED"
    assert pos["after"]["symbol"] == "NEW"
    assert pos["after"]["quantity"] == 100 and pos["after"]["cost_basis_minor"] == 500000


def test_cli_delisting_marks_delisted_and_notifies_the_operator() -> None:
    # AC clause 3: a delisting marks the position delisted AND notifies the operator.
    result = _run_cli(
        ["plan", "--symbol", "DEAD", "--delisting", "--position", "sym=DEAD,qty=100,basis=500000"]
    )
    assert result.returncode == 0, result.stderr
    (pos,) = _outcomes(result.stdout)
    assert pos["result"] == "DELISTED"
    assert pos["position"]["quantity"] == 100, "quantity frozen"
    # ... the operator is notified through the notification subsystem.
    assert pos["notification"]["trigger_kind"] == "CRITICAL_FAILURE"
    assert pos["notification"]["severity"] == "CRITICAL"
    assert "delisted" in pos["notification"]["summary"]
    # ... and the strategy is told.
    assert pos["callback"]["kind"] == "DELISTED"


def test_cli_unapplicable_action_is_manual_review_with_notification() -> None:
    # A fractional reverse split cannot be applied — flagged for the operator.
    result = _run_cli(
        ["plan", "--symbol", "ZZZ", "--split", "1:10", "--position", "sym=ZZZ,qty=5,basis=5000"]
    )
    assert result.returncode == 0, result.stderr
    (pos,) = _outcomes(result.stdout)
    assert pos["result"] == "MANUAL_REVIEW"
    assert pos["reason"] == "QUANTITY_NOT_INTEGRAL"
    assert pos["notification"]["trigger_kind"] == "CRITICAL_FAILURE"
    assert "fractional share" in pos["notification"]["summary"]


def test_cli_successor_collision_is_flagged() -> None:
    # A merger onto a symbol another position already holds is a manual operation.
    result = _run_cli(
        [
            "plan",
            "--symbol",
            "OLD",
            "--merger",
            "NEW:1:1:0",
            "--position",
            "sym=OLD,qty=100,basis=500000",
            "--position",
            "sym=NEW,qty=50,basis=250000",
        ]
    )
    assert result.returncode == 0, result.stderr
    outcomes = {o["symbol"]: o for o in _outcomes(result.stdout)}
    assert outcomes["OLD"]["result"] == "MANUAL_REVIEW"
    assert outcomes["OLD"]["reason"] == "SUCCESSOR_COLLISION"
    assert outcomes["NEW"]["result"] == "UNAFFECTED"


def test_cli_positions_file_is_read(tmp_path: Path) -> None:
    positions_file = tmp_path / "positions.txt"
    positions_file.write_text(
        "# positions for the AAPL split\n"
        "sym=AAPL,qty=100,basis=500000\n"
        "\n"
        "sym=MSFT,qty=50,basis=250000\n"
    )
    result = _run_cli(
        ["plan", "--symbol", "AAPL", "--split", "2:1", "--positions-file", str(positions_file)]
    )
    assert result.returncode == 0, result.stderr
    outcomes = {o["symbol"]: o for o in _outcomes(result.stdout)}
    assert outcomes["AAPL"]["result"] == "ADJUSTED"
    assert outcomes["MSFT"]["result"] == "UNAFFECTED"  # other symbol


def test_cli_unknown_flag_exits_2_without_planning() -> None:
    result = _run_cli(
        [
            "plan",
            "--symbol",
            "AAPL",
            "--split",
            "4:1",
            "--bogus",
            "x",
            "--position",
            "sym=AAPL,qty=100,basis=500000",
        ]
    )
    assert result.returncode == 2
    assert "outcome:" not in result.stdout


def test_cli_sign_inconsistent_position_fails_closed() -> None:
    # A position whose basis sign disagrees with its quantity (a negative average cost)
    # is rejected — a corrupt live-feed record can never enter the planner.
    result = _run_cli(
        ["plan", "--symbol", "AAPL", "--delisting", "--position", "sym=AAPL,qty=100,basis=-5"]
    )
    assert result.returncode == 2
    assert "outcome:" not in result.stdout


def test_cli_duplicate_input_positions_are_all_flagged_for_review() -> None:
    # Two records for one canonical symbol violate the one-position-per-symbol invariant:
    # both must be flagged, never independently remapped into two successor positions.
    result = _run_cli(
        [
            "plan",
            "--symbol",
            "OLD",
            "--merger",
            "NEW:1:1:0",
            "--position",
            "sym=OLD,qty=100,basis=500000",
            "--position",
            "sym=OLD,qty=40,basis=200000",
        ]
    )
    assert result.returncode == 0, result.stderr
    outs = _outcomes(result.stdout)
    assert len(outs) == 2
    assert all(o["result"] == "MANUAL_REVIEW" and o["reason"] == "DUPLICATE_POSITION" for o in outs)
    assert all(o["symbol"] != "NEW" for o in outs), "no fabricated successor remap"


def test_cli_symbol_with_control_character_emits_valid_json(tmp_path: Path) -> None:
    # A symbol carrying a C0 control character (from a malformed positions file) must be
    # `\\uXXXX`-escaped so every `outcome:` line is still parseable JSON — _outcomes()
    # would raise json.JSONDecodeError otherwise.
    positions_file = tmp_path / "positions.txt"
    positions_file.write_text("sym=AA\x01BB,qty=100,basis=500000\n")
    result = _run_cli(
        ["plan", "--symbol", "ZZZ", "--delisting", "--positions-file", str(positions_file)]
    )
    assert result.returncode == 0, result.stderr
    (pos,) = _outcomes(result.stdout)  # parses => the control char was escaped
    assert pos["symbol"] == "AA\x01BB", "the escaped symbol round-trips through JSON"
    assert pos["result"] == "UNAFFECTED"  # the control-char symbol != the ZZZ action
