"""L1 — Unit tests for the gate registry (tools/gates.json + gates_registry_check.py).

The defect these cover: "what must pass?" had three different answers — init.sh ran
62 contract checks, ci.yml ran 32, and run_ci_locally.sh (the self-described mirror
of ci.yml) ran 25. 21 checks were enforced nowhere but a bootstrap script, and 22
took a --require-cargo flag in one runner and not the others, which silently
changed whether a missing toolchain was a failure or a skip.

These pin the registry as the single answer, and pin the validator that stops it
drifting from the checks actually on disk.
"""

from __future__ import annotations

import gates_registry_check as grc
import pytest

pytestmark = pytest.mark.unit


def _reg(checks=None, excluded=None, scopes=None):
    return {
        "scopes": scopes if scopes is not None else {"env": "…", "ci": "…", "ci-rust": "…"},
        "checks": checks if checks is not None else [],
        "excluded": excluded if excluded is not None else [],
    }


# --- the real registry ----------------------------------------------------------
def test_shipped_registry_is_coherent():
    """The registry in the tree must describe the checks in the tree."""
    registry = grc.load_registry()
    problems = grc.audit(registry, grc.discover_checks())
    assert problems == [], problems


def test_every_check_on_disk_is_accounted_for():
    registry = grc.load_registry()
    named = {c["name"] for c in registry["checks"]} | {e["name"] for e in registry["excluded"]}
    assert grc.discover_checks() - named == set()


def test_cargo_strict_scope_carries_the_flag():
    """--require-cargo turns a missing-cargo SKIP into a FAILURE; losing it is silent."""
    registry = grc.load_registry()
    rust = {
        c["name"]: c["scopes"]["ci-rust"] for c in registry["checks"] if "ci-rust" in c["scopes"]
    }
    assert rust, "no cargo-strict scope registered"
    assert all(argv == ["--require-cargo"] for argv in rust.values())
    # These are the ones init.sh used to run strictly and CI never did.
    assert {"sim_fill_check", "determinism_check", "backtest_check"} <= set(rust)


# --- the validator actually fires -----------------------------------------------
def test_unregistered_check_on_disk_is_a_problem():
    problems = grc.audit(_reg(), {"brand_new_check"})
    assert any("brand_new_check" in p and "neither" in p for p in problems)


def test_registered_check_with_no_file_is_a_problem():
    reg = _reg(checks=[{"name": "ghost_check", "scopes": {"ci": []}, "why": "x"}])
    problems = grc.audit(reg, set())
    assert any("ghost_check" in p and "does not exist" in p for p in problems)


def test_duplicate_name_is_a_problem():
    reg = _reg(
        checks=[{"name": "critic_check", "scopes": {"ci": []}, "why": "x"}],
        excluded=[{"name": "critic_check", "why": "y"}],
    )
    problems = grc.audit(reg, set())
    assert any("listed twice" in p for p in problems)


def test_unknown_scope_is_a_problem():
    reg = _reg(checks=[{"name": "critic_check", "scopes": {"nope": []}, "why": "x"}])
    problems = grc.audit(reg, set())
    assert any("expected one of" in p for p in problems)


def test_entry_without_a_reason_is_a_problem():
    reg = _reg(checks=[{"name": "critic_check", "scopes": {"ci": []}, "why": "  "}])
    problems = grc.audit(reg, set())
    assert any("no `why`" in p for p in problems)


def test_arg_containing_whitespace_is_a_problem():
    """verify_contracts.sh word-splits argv; an embedded space would mis-pass it."""
    reg = _reg(checks=[{"name": "critic_check", "scopes": {"ci": ["--flag value"]}, "why": "x"}])
    problems = grc.audit(reg, set())
    assert any("whitespace" in p for p in problems)


def test_missing_scopes_object_is_a_problem():
    reg = _reg(checks=[{"name": "critic_check", "why": "x"}])
    problems = grc.audit(reg, set())
    assert any("no `scopes`" in p for p in problems)


# --- scope resolution -----------------------------------------------------------
def test_scoped_emits_name_and_argv():
    reg = _reg(
        checks=[
            {"name": "a_check", "scopes": {"ci": [], "ci-rust": ["--require-cargo"]}, "why": "x"},
            {"name": "b_check", "scopes": {"env": []}, "why": "y"},
        ]
    )
    assert grc.scoped(reg, "ci") == ["a_check"]
    assert grc.scoped(reg, "ci-rust") == ["a_check --require-cargo"]
    assert grc.scoped(reg, "env") == ["b_check"]


def test_scopes_are_independent_not_hierarchical():
    """`env` is a deliberate subset, not a prefix of `ci` — they resolve separately."""
    registry = grc.load_registry()
    env = set(grc.scoped(registry, "env"))
    ci = set(grc.scoped(registry, "ci"))
    assert env and ci and env < ci
