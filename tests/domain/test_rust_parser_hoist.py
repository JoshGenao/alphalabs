"""L7 domain safety test for the shared Rust-source parsers
(``tools/_rust_parser.py``).

The deterministic critic's ``safety:paired-test-required`` rule fires
on changes to ``tools/{connectivity,ingestion_validation,
orchestrator_lifecycle,pacing_budget,subscription_limit}_check.py``
because those paths match ``SAFETY_PATH_RE``. Every per-feature gate
(ERR-1..6, SRS-ORCH-001) depends on these check scripts producing
PASS evidence; the check scripts in turn depend on the six brace-
matching helpers in ``tools/_rust_parser.py``.

This test anchors the helpers' semantic contract at the domain
layer so a future refactor that silently breaks one of them (a
regex tweak, a depth-counting off-by-one, a string-literal escape
mistake) is caught here rather than in production CI for a feature
that happens to be the first to use the broken helper.

Trace: SRS-ARCH-002 (dependency direction), AC-12 (orchestrator
container isolation), SyRS SYS-64 (structured-error vocabulary).
The helpers do not enforce any of those clauses directly — they
extract the source regions the per-feature check scripts inspect
to enforce them. Breaking a helper silently disarms every gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from _rust_parser import (  # noqa: E402
    RustParserError,
    _enum_body,
    _fn_block,
    _match_arm,
    _struct_body,
    _trait_body,
    _variant_arm,
)

pytestmark = [pytest.mark.domain, pytest.mark.safety]


FIXTURE = """
pub struct StructuredOrderError {
    pub category: OrderErrorCategory,
    pub message: String,
}

pub enum OrderErrorCategory {
    NonLiveStrategySubmission,
    ConnectivityBlocked,
    MarketDataStale,
}

pub trait BrokerageConnectivity {
    fn state(&self) -> ConnectivityState;
    fn request_reconnect(&self);
}

pub fn submit_live_order(req: OrderRequest) -> Result<OrderReceipt, StructuredOrderError> {
    match req.mode {
        StrategyMode::Live => broker.submit_order(req),
        StrategyMode::Paper => Err(StructuredOrderError {
            category: OrderErrorCategory::NonLiveStrategySubmission,
            message: "paper path".to_string(),
        }),
    }
}

pub fn launch_outcome(readiness: LaunchReadiness) -> StrategyLaunchOutcome {
    match readiness {
        LaunchReadiness::ReadyWithinDeadline { elapsed_millis } => {
            StrategyLaunchOutcome::new(elapsed_millis)
        },
        LaunchReadiness::DeadlineExceeded => {
            runtime.destroy(&strategy_id);
            sink.record(event);
        },
    }
}
"""


def test_struct_body_extracts_struct_fields() -> None:
    body = _struct_body(FIXTURE, "StructuredOrderError")
    assert "pub category: OrderErrorCategory" in body
    assert "pub message: String" in body
    assert "OrderErrorCategory" not in body.split("pub category")[0]


def test_enum_body_extracts_variants() -> None:
    body = _enum_body(FIXTURE, "OrderErrorCategory")
    assert "NonLiveStrategySubmission" in body
    assert "ConnectivityBlocked" in body
    assert "MarketDataStale" in body
    assert "pub struct" not in body


def test_trait_body_extracts_methods() -> None:
    body = _trait_body(FIXTURE, "BrokerageConnectivity")
    assert "fn state" in body
    assert "fn request_reconnect" in body
    assert "pub enum" not in body


def test_fn_block_extracts_function_body() -> None:
    body = _fn_block(FIXTURE, "submit_live_order")
    assert "match req.mode" in body
    assert "StrategyMode::Live" in body
    assert "pub fn launch_outcome" not in body


def test_match_arm_extracts_unit_variant_arm() -> None:
    fn_body = _fn_block(FIXTURE, "submit_live_order")
    arm = _match_arm(fn_body, "StrategyMode::Live")
    assert "broker.submit_order" in arm
    assert "NonLiveStrategySubmission" not in arm


def test_variant_arm_extracts_struct_variant_arm() -> None:
    fn_body = _fn_block(FIXTURE, "launch_outcome")
    arm = _variant_arm(fn_body, "LaunchReadiness::ReadyWithinDeadline")
    assert "StrategyLaunchOutcome::new" in arm
    assert "elapsed_millis" in arm
    assert "runtime.destroy" not in arm


def test_variant_arm_extracts_unit_variant_arm() -> None:
    fn_body = _fn_block(FIXTURE, "launch_outcome")
    arm = _variant_arm(fn_body, "LaunchReadiness::DeadlineExceeded")
    assert "runtime.destroy" in arm
    assert "sink.record" in arm
    assert "StrategyLaunchOutcome::new" not in arm


def test_missing_construct_raises_parser_error() -> None:
    with pytest.raises(RustParserError, match="missing function"):
        _fn_block(FIXTURE, "nonexistent_fn")
    with pytest.raises(RustParserError, match="missing public struct"):
        _struct_body(FIXTURE, "NonexistentStruct")
    with pytest.raises(RustParserError, match="missing public enum"):
        _enum_body(FIXTURE, "NonexistentEnum")
    with pytest.raises(RustParserError, match="missing public trait"):
        _trait_body(FIXTURE, "NonexistentTrait")


def test_match_arm_respects_nested_braces_and_strings() -> None:
    body = (
        'Pattern => { call(\"a => b\", { nested: 1 }, [1, 2]); next() }, '
        "Other => skip,"
    )
    arm = _match_arm(body, "Pattern")
    assert "nested: 1" in arm
    assert "Other" not in arm
