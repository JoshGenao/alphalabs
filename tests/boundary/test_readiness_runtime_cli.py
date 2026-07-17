"""L4 boundary tests — SRS-MD-006 probes over the REAL Rust CLIs and the
operator ``runtime_cli`` end to end (fixture stores, provider mocks, file
reads, persisted-output inspection — the feature's own Step-2 contexts)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from atp_config import REQUIRED_KEYS
from atp_readiness.probes import JsonlAlertSink, TierCliStorageProbe
from atp_readiness.runtime import ReadinessAlertKind
from atp_readiness.runtime_cli import main as runtime_cli_main
from atp_reliability.restart import SubCheckStatus

REPO_ROOT = Path(__file__).resolve().parents[2]
TIER_CLI = REPO_ROOT / "target" / "debug" / "data008_tier_cli"
COVERAGE_CLI = REPO_ROOT / "target" / "debug" / "data011_coverage_cli"

#: A fixed operator instant (epoch-s) — Monday 2025-06-16 18:00 ET-ish; the
#: exact calendar session is resolved by the real UsEquityTradingCalendar.
NOW_S = 1_750_100_000
NOW_NS = NOW_S * 1_000_000_000

pytestmark = pytest.mark.skipif(
    not (TIER_CLI.exists() and COVERAGE_CLI.exists()),
    reason="cargo-built data CLIs missing; build with `cargo build -p atp-data --bins`",
)


class RecordingSink:
    def __init__(self) -> None:
        self.alerts = []

    def dispatch(self, alert) -> None:
        self.alerts.append(alert)


@pytest.fixture()
def stores(tmp_path: Path) -> dict[str, Path]:
    ssd = tmp_path / "ssd"
    nas = tmp_path / "nas"
    ssd.mkdir()
    nas.mkdir()
    # Seed the coverage frontier at NOW (fresh) for AAPL through the real CLI.
    subprocess.run(
        [
            str(COVERAGE_CLI),
            "assert-coverage",
            "--dir",
            str(ssd),
            "--symbol",
            "AAPL",
            "--through",
            str(NOW_S),
            "--init",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return {"ssd": ssd, "nas": nas, "tmp": tmp_path}


def _base_fixtures(tmp_path: Path) -> dict[str, Path]:
    ib = tmp_path / "ib.json"
    ib.write_text(json.dumps({"connectivity": True, "account_data": True}))
    services = tmp_path / "services.json"
    services.write_text(
        json.dumps(
            {
                "execution_engine": True,
                "internal_simulation_engine": True,
                "data_layer": True,
                "notification_subsystem": True,
                "dashboard": True,
            }
        )
    )
    paper = tmp_path / "paper.json"
    paper.write_text(
        json.dumps({"market_data_subscription_manager": True, "internal_simulation_engine": True})
    )
    return {"ib": ib, "services": services, "paper": paper}


def _cli(stores: dict[str, Path], fixtures: dict[str, Path], monkeypatch, *extra: str) -> int:
    env = {s.name: s.default for s in REQUIRED_KEYS if s.default is not None}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return runtime_cli_main(
        [
            "--ssd",
            str(stores["ssd"]),
            "--nas",
            str(stores["nas"]),
            "--watchlist",
            "AAPL",
            "--ib-fixture",
            str(fixtures["ib"]),
            "--services-fixture",
            str(fixtures["services"]),
            "--alerts",
            str(stores["tmp"] / "alerts.jsonl"),
            "--now",
            str(NOW_NS),
            *extra,
        ]
    )


def test_tier_probe_reports_reachable_nas_as_pass(stores, monkeypatch) -> None:
    sink = RecordingSink()
    probe = TierCliStorageProbe(stores["ssd"], stores["nas"], now_ts=NOW_S)
    ssd, nas = probe.observe(alert_sink=sink, timestamp_ns=NOW_NS)
    assert ssd.status is SubCheckStatus.PASS
    assert nas.status is SubCheckStatus.PASS
    assert not sink.alerts


def test_tier_probe_degrades_unreachable_nas_with_bound_alert(stores) -> None:
    sink = RecordingSink()
    probe = TierCliStorageProbe(stores["ssd"], stores["tmp"] / "no-such-nas", now_ts=NOW_S)
    ssd, nas = probe.observe(alert_sink=sink, timestamp_ns=NOW_NS)
    assert ssd.status is SubCheckStatus.PASS
    assert nas.status is SubCheckStatus.DEGRADED and nas.alert_raised is True
    assert [a.kind for a in sink.alerts] == [ReadinessAlertKind.NAS_DEGRADED_MODE]

    # A sink that cannot deliver makes degraded UNACCEPTABLE (alert_raised
    # False), which the fold then fails — bound to the dispatch outcome.
    class RaisingSink:
        def dispatch(self, alert) -> None:
            raise OSError("alert channel down")

    _ssd, nas = probe.observe(alert_sink=RaisingSink(), timestamp_ns=NOW_NS)
    assert nas.status is SubCheckStatus.DEGRADED and nas.alert_raised is False


def test_runtime_cli_ready_path_and_persisted_alerts(stores, monkeypatch, capsys) -> None:
    fixtures = _base_fixtures(stores["tmp"])
    exit_code = _cli(stores, fixtures, monkeypatch, "--paper-fixture", str(fixtures["paper"]))
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "state:ready" in out and "paper_ready:true" in out
    assert (stores["tmp"] / "alerts.jsonl").exists() is False or not (
        stores["tmp"] / "alerts.jsonl"
    ).read_text().strip(), "an all-pass run must not dispatch alerts"


def test_runtime_cli_blocks_on_ib_failure_with_persisted_alert(stores, monkeypatch, capsys) -> None:
    fixtures = _base_fixtures(stores["tmp"])
    fixtures["ib"].write_text(json.dumps({"connectivity": False, "account_data": True}))
    exit_code = _cli(stores, fixtures, monkeypatch)
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "state:pre_trade_blocked" in out
    alerts = JsonlAlertSink(stores["tmp"] / "alerts.jsonl").read()
    assert [a["kind"] for a in alerts] == ["subcheck_failure"]
    assert alerts[0]["key"] == "ib_connectivity"


def test_runtime_cli_stale_frontier_blocks(stores, monkeypatch, capsys) -> None:
    # Advance the operator clock ~9 days past the seeded frontier: the most
    # recent completed session close is now far after it => stale => blocked.
    fixtures = _base_fixtures(stores["tmp"])
    env = {s.name: s.default for s in REQUIRED_KEYS if s.default is not None}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    later_ns = (NOW_S + 9 * 86_400) * 1_000_000_000
    exit_code = runtime_cli_main(
        [
            "--ssd",
            str(stores["ssd"]),
            "--nas",
            str(stores["nas"]),
            "--watchlist",
            "AAPL",
            "--ib-fixture",
            str(fixtures["ib"]),
            "--services-fixture",
            str(fixtures["services"]),
            "--alerts",
            str(stores["tmp"] / "alerts.jsonl"),
            "--now",
            str(later_ns),
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "subcheck.data_layer_ssd:fail" in out


def test_runtime_cli_override_releases_hold_and_alerts(stores, monkeypatch, capsys) -> None:
    fixtures = _base_fixtures(stores["tmp"])
    fixtures["ib"].write_text(json.dumps({"connectivity": False, "account_data": False}))
    exit_code = _cli(
        stores,
        fixtures,
        monkeypatch,
        "--override-actor",
        "ops",
        "--override-reason",
        "known outage",
        "--override-audit-id",
        "AUD-9",
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "state:overridden" in out
    kinds = [a["kind"] for a in JsonlAlertSink(stores["tmp"] / "alerts.jsonl").read()]
    assert kinds.count("subcheck_failure") == 2
    assert kinds[-1] == "operator_override"


def test_evidence_probe_rejects_not_ok_and_failed_result_lines(tmp_path: Path) -> None:
    # Codex R1: a TAP-style "not ok" line (or a FAILED harness line) must
    # never substring-match as success.
    from atp_readiness.probes import EvidenceFileIbProbe
    from atp_reliability.restart import SubCheckStatus as Status

    generated_ns = NOW_NS
    good = {
        "schema_version": 1,
        "test": "paper_account_round_trip",
        "returncode": 0,
        "result_line": "test result: ok. 1 passed; 0 failed; 0 ignored; finished in 1.65s",
        "generated_at": "2025-06-16T18:53:20Z",  # == NOW_S epoch
    }
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(good))
    probe = EvidenceFileIbProbe(evidence, now_ns=lambda: generated_ns + 60_000_000_000)
    conn, account = probe.observe()
    assert conn.status is Status.PASS and account.status is Status.PASS

    for bad_line in (
        "not ok 1 - paper_account_round_trip",
        "test result: FAILED. 0 passed; 1 failed; finished in 1.65s",
        "test result: ok. 1 passed; 1 failed; 0 ignored",
        "",
    ):
        evidence.write_text(json.dumps({**good, "result_line": bad_line}))
        conn, account = EvidenceFileIbProbe(
            evidence, now_ns=lambda: generated_ns + 60_000_000_000
        ).observe()
        assert conn.status is Status.FAIL, bad_line
        assert account.status is Status.FAIL, bad_line


def test_evidence_probe_rejects_stale_or_future_evidence(tmp_path: Path) -> None:
    # Codex R2: evidence proves a PAST session — beyond the freshness bound
    # (or stamped in the future, or missing its stamp) it must fail closed.
    from atp_readiness.probes import EVIDENCE_MAX_AGE_NS, EvidenceFileIbProbe
    from atp_reliability.restart import SubCheckStatus as Status

    good = {
        "schema_version": 1,
        "test": "paper_account_round_trip",
        "returncode": 0,
        "result_line": "test result: ok. 1 passed; 0 failed; 0 ignored; finished in 1.65s",
        "generated_at": "2025-06-16T18:53:20Z",
    }
    generated_ns = NOW_NS
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(good))

    fresh = EvidenceFileIbProbe(evidence, now_ns=lambda: generated_ns + EVIDENCE_MAX_AGE_NS)
    assert fresh.observe()[0].status is Status.PASS, "exactly at the bound is fresh"

    stale = EvidenceFileIbProbe(evidence, now_ns=lambda: generated_ns + EVIDENCE_MAX_AGE_NS + 1)
    assert stale.observe()[0].status is Status.FAIL, "one ns past the bound is stale"

    future = EvidenceFileIbProbe(evidence, now_ns=lambda: generated_ns - 1)
    assert future.observe()[0].status is Status.FAIL, "future-stamped evidence fails closed"

    evidence.write_text(json.dumps({k: v for k, v in good.items() if k != "generated_at"}))
    unstamped = EvidenceFileIbProbe(evidence, now_ns=lambda: generated_ns)
    assert unstamped.observe()[0].status is Status.FAIL, "missing stamp fails closed"


def test_runtime_cli_paper_prerequisite_failure_fails_the_command(
    stores, monkeypatch, capsys
) -> None:
    # Codex R1: when the operator asked for the paper gate, an unmet paper
    # prerequisite must FAIL the command even though the live gate is ready.
    fixtures = _base_fixtures(stores["tmp"])
    fixtures["paper"].write_text(
        json.dumps({"internal_simulation_engine": True})  # subscription manager missing
    )
    exit_code = _cli(stores, fixtures, monkeypatch, "--paper-fixture", str(fixtures["paper"]))
    out = capsys.readouterr().out
    assert exit_code == 1, "paper hold must not exit 0"
    assert "paper_ready:false" in out
    kinds = [a["kind"] for a in JsonlAlertSink(stores["tmp"] / "alerts.jsonl").read()]
    assert "paper_prerequisite_failure" in kinds


def test_runtime_cli_refuses_bad_input(stores, monkeypatch, capsys) -> None:
    fixtures = _base_fixtures(stores["tmp"])
    for extra in (
        ["--frobnicate", "x"],
        ["--now", "not-a-number"],
        ["--ib-evidence", "also.json"],  # both IB sources at once
    ):
        exit_code = _cli(stores, fixtures, monkeypatch, *extra)
        assert exit_code == 2, f"input {extra} must be refused"
    capsys.readouterr()
