"""L1 — the Compose project name an SRS-SEC-004 integration test derives.

The function lives in tests/integration/, whose module-level
`pytestmark = pytest.mark.integration` means anything defined beside it is skipped
unless ATP_RUN_INTEGRATION=1. The bug it guards against is a NAME-BUILDING bug —
pure string work, no docker — so the guard belongs where it runs on every commit
rather than only in the gated workflow that already caught it once.
"""

import importlib.util
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[1] / "integration" / "test_jupyter_isolation_inspect.py"
_spec = importlib.util.spec_from_file_location("_sec004_mod", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_compose_project = _mod._compose_project

#: Compose builds image refs as `<project>-<service>`; this is what the daemon accepts.
_DOCKER_REF = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")


def _image(dir_name: str) -> str:
    return f"{_compose_project(dir_name)}-phase1-jupyter"


def test_the_exact_name_that_turned_ci_red_is_now_valid():
    """`unable to get image 'atpsec004atpsec004ntkxt33_-phase1-jupyter':
    invalid reference format` — tempfile.mkdtemp draws from [a-z0-9_], so its
    suffix ended in `_` and the appended `-phase1-jupyter` made the reference
    illegal."""
    assert _DOCKER_REF.fullmatch(_image("atp-sec004-ntkxt33_"))


@pytest.mark.parametrize(
    "dir_name",
    [
        "atp-sec004-ntkxt33_",  # the observed failure
        "atp-sec004-_____",  # all separators
        "atp-sec004-_a_b_c_",  # separators at both ends
        "atp-sec004-ABC-123",  # uppercase is illegal in a reference
        "atp-sec004-x",  # short
        "atp-sec004-" + "z" * 60,  # long enough to hit the truncation
    ],
    ids=["observed", "all_seps", "seps_both_ends", "uppercase", "short", "truncated"],
)
def test_no_temp_dir_name_can_produce_an_illegal_image_reference(dir_name):
    """The truncation is what made this a lottery: a name could be legal until the
    20-character cut happened to land on a separator."""
    project = _compose_project(dir_name)
    assert _DOCKER_REF.fullmatch(project), f"project {project!r} is not a legal reference"
    assert _DOCKER_REF.fullmatch(_image(dir_name))
    assert not project.endswith(("_", "-", "."))


def test_the_name_still_identifies_the_run():
    """Sanitising must not collapse every temp dir to one project — two concurrent
    runs sharing a project name would tear down each other's containers."""
    a, b = _compose_project("atp-sec004-aaa111"), _compose_project("atp-sec004-bbb222")
    assert a != b
    assert a.startswith("atpsec004") and b.startswith("atpsec004")
