"""L1 — feature_list.json agrees with the release scope in docs/SRS.md §3.1.

The scheduler reads `release` from feature_list.json; people read the SRS. If
the two drift, an agent works on deferred scope or the MVP silently shrinks.
This fails for the whole class: any feature, any direction.
"""

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SRS = (ROOT / "docs" / "SRS.md").read_text(encoding="utf-8")
FEATURES = json.loads((ROOT / "feature_list.json").read_text(encoding="utf-8"))
SECTION = SRS[SRS.index("### 3.1 Releases") : SRS.index("## 4. Software Architecture")]


def _ids(block: str) -> set:
    return set(re.findall(r"^\| (\S+) \|", block, re.M)) - {"ID", "----"}


R2_IDS = _ids(SECTION.split("#### R2 requirements")[1].split("####")[0])
SPLIT_IDS = _ids(SECTION.split("#### MVP acceptance criteria for Split requirements")[1])


def test_srs_section_parses():
    # Guards the guard: an empty parse would make every check below vacuous.
    assert R2_IDS and SPLIT_IDS and not (R2_IDS & SPLIT_IDS)


def test_every_feature_has_a_known_release():
    bad = {f["id"]: f.get("release") for f in FEATURES if f.get("release") not in ("MVP", "R2")}
    assert not bad


def test_r2_features_match_the_srs_exactly():
    tagged = {f["id"] for f in FEATURES if f.get("release") == "R2"}
    assert tagged == R2_IDS


def test_srs_release_ids_are_real_features():
    ids = {f["id"] for f in FEATURES}
    assert (R2_IDS | SPLIT_IDS) <= ids


def test_open_split_features_carry_mvp_criteria():
    # An open Split feature verified against its full v0.4 criteria would build
    # R2 scope; its Step 3 must state the narrowed MVP criteria.
    stale = [
        f["id"]
        for f in FEATURES
        if f["id"] in SPLIT_IDS
        and f.get("passes") is not True
        and not f["steps"][2].startswith(
            "Step 3: Verify MVP acceptance criteria (docs/SRS.md §3.1)"
        )
    ]
    assert not stale
