"""L1 — Deriving verification_method from the acceptance criterion.

The field decides whether a feature may ever close as `complete` without a human
attestation, so a wrong `solo` is a faked green and a wrong `live-ib` spends the
scarcest resource on the board — an operator's live-IB window. The derivation used
to read templated boilerplate and produced solo 9 / non-solo 111 on a 120-feature
corpus. These tests pin what replaced it: the acceptance criterion (the only
feature-specific prose), read with an eye for resources that are NAMED but not
NEEDED, and a tracked override file where a human overrules it with a reason.
"""

import json

import classify_verification as cv
import pytest

pytestmark = pytest.mark.unit


def _feat(ac, fid="F-1", **kw):
    base = {
        "id": fid,
        "steps": [
            "Step 1: Run ./init.sh and confirm the development environment reports Environment ready.",
            "Step 2: Exercise the feature using fixtures.",
            f"Step 3: Verify acceptance criteria: {ac}",
            "Step 4: Record objective evidence.",
        ],
    }
    base.update(kw)
    return base


# --- the AC is read, not the template ---------------------------------------
def test_ac_text_strips_the_template_prefix():
    assert cv.ac_text(_feat("the widget spins.")).strip() == "the widget spins."


def test_ac_text_is_empty_when_there_is_no_third_step():
    assert cv.ac_text({"id": "X", "steps": ["a", "b"]}) == ""


# --- precedence is by binding constraint ------------------------------------
def test_a_real_ib_dependency_outranks_a_dashboard_one():
    """The single-live invariant is what actually serializes sessions."""
    ac = "orders submitted to the ib gateway are displayed on the dashboard."
    assert cv.derive_from_ac(_feat(ac))[0] == "live-ib"


def test_a_dashboard_outranks_a_container():
    ac = "the dashboard shows container status for each strategy."
    assert cv.derive_from_ac(_feat(ac))[0] == "e2e"


def test_an_ac_naming_nothing_external_returns_none():
    """None, not 'solo' — the caller must not read a miss as permission."""
    assert cv.derive_from_ac(_feat("renko bars are generated from tick data.")) is None


# --- a resource NAMED is not a resource NEEDED ------------------------------
# Roughly a third of the ACs mentioning IB mention it to require the system NOT
# touch it. Reading those as live-ib is the exact inverse of the old " ib " keyword
# scan, and it costs an operator window per feature.
@pytest.mark.parametrize(
    "ac",
    [
        "paper strategy orders never create ib orders.",
        "jupyter cannot submit live orders or read brokerage credentials.",
        "notebook code renders plots without access to live order submission.",
        "p&l is isolated per paper strategy and independent of ib account positions.",
    ],
    ids=["never", "cannot", "without", "independent_of"],
)
def test_a_negated_ib_reference_is_not_a_live_ib_dependency(ac):
    got = cv.derive_from_ac(_feat(ac))
    assert got is None or got[0] != "live-ib"


def test_a_rejection_criterion_is_not_a_live_ib_dependency():
    """Refusing a live submission is proven by showing the refusal, not by having
    a gateway to refuse against — ERR-2 is a fault-injection criterion."""
    ac = "reject live order submission with connectivity_blocked and alert dashboard."
    assert cv.derive_from_ac(_feat(ac))[0] == "e2e"


def test_a_configuration_mention_is_not_a_live_session():
    """SRS-ARCH-004's 'ib gateway integration configuration' is a compose entry —
    and phase1-ib-gateway is a `sleep 3600` placeholder.

    The SSD tier in the same sentence must survive: the qualifier binds to the
    gateway, not to everything after it. A wider window neutralised both, and the
    feature fell through with its genuinely external resources unseen.
    """
    ac = "compose starts core services, ib gateway integration configuration, and ssd paths."
    assert cv.derive_from_ac(_feat(ac)) == ("integration", "ssd")


def test_the_search_continues_past_a_neutralised_hit():
    """'jupyter ... cannot submit live orders' must land on e2e — it does need
    Jupyter — rather than falling all the way through to nothing."""
    ac = "jupyter has read-only access and cannot submit live orders."
    assert cv.derive_from_ac(_feat(ac))[0] == "e2e"


def test_a_negator_after_the_phrase_does_not_neutralise_it():
    """No trailing negators, deliberately. SRS-SIM-004's 'restored within 30
    seconds of CONTAINER RESTART, excluding warm-up' was silently turned solo by a
    trailing scan: the exclusion applies to the warm-up, not the container. A rule
    that turns one correct non-solo call into a wrong solo one is worse than the
    miss it fixes, because solo is the permissive answer."""
    ac = "state is restored within 30 seconds of container restart, excluding warm-up."
    assert cv.derive_from_ac(_feat(ac))[0] == "integration"


# --- operator overrides -----------------------------------------------------
@pytest.fixture
def override_file(tmp_path, monkeypatch):
    path = tmp_path / "overrides.json"
    monkeypatch.setattr(cv, "OVERRIDE_FILE", path)
    return path


def test_an_override_is_loaded_with_its_reason(override_file):
    override_file.write_text(
        json.dumps({"$comment": ["ignored"], "F-1": {"method": "solo", "why": "static check"}}),
        encoding="utf-8",
    )
    got = cv.load_overrides()
    assert got == {"F-1": {"method": "solo", "why": "static check"}}  # $comment dropped


def test_a_missing_override_file_is_simply_no_overrides(override_file):
    assert cv.load_overrides() == {}


def test_a_corrupt_override_file_is_fatal_not_empty(override_file):
    """Rule 3. Treating corrupt as 'no overrides' would silently re-derive every
    hand-made call from the prose the derivation exists to distrust."""
    override_file.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit, match="corrupt"):
        cv.load_overrides()


def test_an_override_with_an_invalid_method_is_rejected(override_file):
    override_file.write_text(
        json.dumps({"F-1": {"method": "probably-fine", "why": "x"}}), encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="expected one of"):
        cv.load_overrides()


def test_an_override_with_no_reason_is_rejected(override_file):
    """An override with no reason is exactly the override this file replaces."""
    override_file.write_text(json.dumps({"F-1": {"method": "solo"}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="no 'why'"):
        cv.load_overrides()


# --- the shipped override file must stay valid ------------------------------
def test_the_repo_override_file_parses_and_every_entry_is_justified():
    """It is applied to feature_list.json, so a typo here silently reclassifies a
    real feature. Uses the real path, not the fixture."""
    overrides = cv.load_overrides()
    assert overrides, "the repo ships overrides; loading them yielded nothing"
    ids = {f["id"] for f in cv.load_features()}
    unknown = sorted(set(overrides) - ids)
    assert not unknown, f"override names features that do not exist: {unknown}"
    for fid, entry in overrides.items():
        assert len(entry["why"]) > 40, f"{fid}: the reason is too thin to audit"


# --- the observation ratchet ------------------------------------------------
def test_a_serialized_note_proposes_but_never_pins():
    """One `Outcome: serialized` note fed BOTH the claim-pool filter and this
    derivation, so a single wedged-gateway session permanently reclassified
    SRS-MD-003 — whose own step 2 says 'fixture market data, provider mocks' — and
    nothing could undo either half. An observation may propose; only a human pins."""
    feat = _feat("the widget spins.", fid="F-1")
    method, why, needs_review = cv.derive(feat, {"F-1": "outcome: serialized — the gateway wedged"})
    assert needs_review is True
    assert "REVIEW" in why
