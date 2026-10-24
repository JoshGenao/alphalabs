"""feature_list.json stays in the one form its writers produce.

Three tools rewrite the whole file - `close_feature.py` (the passes flip),
`evidence.py stamp-pre-gate` and `classify_verification.py apply` - and each
serialises with `json.dumps(features, indent=2)`, which escapes every
non-ASCII character. `close_feature.py` promises a "minimal-diff write" on
that basis: a flip changes one line.

0280b22 committed the file with 14 literal non-ASCII characters (`§`), from
a one-off script. Nothing failed at that commit. The next close, SRS-RESV-006
in 2c70bec, re-escaped all of them, and its one-line flip arrived as 13
changed lines. So the form is pinned here, where an out-of-band write fails
at its own commit instead of in the next operator's close.
"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
FEATURE_FILE = ROOT / "feature_list.json"

sys.path.insert(0, str(ROOT / "tools"))
import close_feature  # noqa: E402


def _writer_form(raw: str) -> str:
    """What every writer of the file would write back for `raw`."""
    return json.dumps(json.loads(raw), indent=2) + ("\n" if raw.endswith("\n") else "")


def _changed_lines(before: str, after: str) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]


def test_feature_list_is_in_the_form_its_writers_produce() -> None:
    raw = FEATURE_FILE.read_text(encoding="utf-8")
    drift = _changed_lines(raw, _writer_form(raw))
    assert not drift, (
        f"feature_list.json differs from the json.dumps(indent=2) form on {len(drift) // 2} "
        "line(s), so the next tool write will rewrite them too. Re-serialise it with "
        "json.dumps(data, indent=2) (no ensure_ascii=False). First: "
        f"{drift[0][:160]}"
    )


def test_a_flip_changes_exactly_one_line(tmp_path: Path) -> None:
    raw = _writer_form(FEATURE_FILE.read_text(encoding="utf-8"))
    # Non-vacuity: the file holds escaped non-ASCII text, the case that broke.
    assert "\\u00a7" in raw and "\\u2014" in raw
    target = next(f["id"] for f in json.loads(raw) if f.get("passes") is False)
    copy = tmp_path / "feature_list.json"
    copy.write_text(raw, encoding="utf-8")

    with mock.patch.object(close_feature, "FEATURE_FILE", copy):
        assert close_feature.flip_passes(target, dry_run=False)

    assert _changed_lines(raw, copy.read_text(encoding="utf-8")) == [
        '-    "passes": false,',
        '+    "passes": true,',
    ]
