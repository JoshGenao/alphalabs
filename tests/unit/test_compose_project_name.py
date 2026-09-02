"""L1 — the Compose project name the container-isolation integration tests derive.

Those tests live under `pytestmark = pytest.mark.integration`, so they are
skipped unless ATP_RUN_INTEGRATION=1. The bug they guard against is a
NAME-BUILDING bug — pure string work, no docker — so the guard belongs where it
runs on every commit rather than only in the gated workflow that caught it
twice.

TWICE, and that is the point of this file's second half. The original fix landed
in `test_jupyter_isolation_inspect.py` and stopped there; its sibling
`test_strategy_container_inspect.py` kept a byte-identical copy of the bug and
turned `main` red the same way months later, on a commit that changed one line
of `feature_list.json`. A fix applied to one call site is not applied to the
class (CLAUDE.md rule 1), so the builder is now shared and
`test_no_integration_test_builds_its_own_project_name` fails if a third copy
appears.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.integration.compose_project import DOCKER_REF as _DOCKER_REF
from tests.integration.compose_project import compose_project

pytestmark = pytest.mark.unit

_INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "integration"

#: The two requirements that stand up a Compose stack to inspect it.
_PREFIXES = ("atpsec003", "atpsec004")

#: `docker compose -p <project>` specifically. A bare ``"-p"`` scan matched
#: `cargo test -p <crate>` in half the integration suite — a guard that flags
#: nine innocent files is one nobody reads.
_COMPOSE_WITH_PROJECT = re.compile(r'"docker",\s*"compose",\s*"-p"')


def _shells_compose_with_project(text: str) -> bool:
    return bool(_COMPOSE_WITH_PROJECT.search(text))


def _image(prefix: str, dir_name: str, service: str) -> str:
    return f"{compose_project(prefix, dir_name)}-{service}"


def test_the_exact_names_that_turned_ci_red_are_now_valid():
    """Both observed failures, verbatim.

    `unable to get image 'atpsec004atpsec004ntkxt33_-phase1-jupyter'` and
    `unable to get image 'atpsec003atpsec0039kn9veb_-phase1-strategy-runtime'`
    — tempfile.mkdtemp draws from [a-z0-9_], so a suffix ending in `_` made the
    appended `-<service>` illegal.
    """

    assert _DOCKER_REF.fullmatch(_image("atpsec004", "atp-sec004-ntkxt33_", "phase1-jupyter"))
    assert _DOCKER_REF.fullmatch(
        _image("atpsec003", "atp-sec003-9kn9veb_", "phase1-strategy-runtime")
    )


@pytest.mark.parametrize("prefix", _PREFIXES)
@pytest.mark.parametrize(
    "dir_name",
    [
        "atp-secXXX-ntkxt33_",  # the observed failure shape
        "atp-secXXX-_____",  # all separators
        "atp-secXXX-_a_b_c_",  # separators at both ends
        "atp-secXXX-ABC-123",  # uppercase is illegal in a reference
        "atp-secXXX-x",  # short
        "atp-secXXX-" + "z" * 60,  # long enough to hit the truncation
    ],
    ids=["observed", "all_seps", "seps_both_ends", "uppercase", "short", "truncated"],
)
def test_no_temp_dir_name_can_produce_an_illegal_image_reference(prefix, dir_name):
    """The truncation is what made this a lottery: a name could be legal until the
    20-character cut happened to land on a separator."""

    project = compose_project(prefix, dir_name)
    assert _DOCKER_REF.fullmatch(project), f"project {project!r} is not a legal reference"
    assert _DOCKER_REF.fullmatch(_image(prefix, dir_name, "phase1-jupyter"))
    assert _DOCKER_REF.fullmatch(_image(prefix, dir_name, "phase1-strategy-runtime"))
    assert not project.endswith(("_", "-", "."))


@pytest.mark.parametrize("prefix", _PREFIXES)
def test_the_name_still_identifies_the_run(prefix):
    """Sanitising must not collapse every temp dir to one project — two concurrent
    runs sharing a project name would tear down each other's containers."""

    a, b = compose_project(prefix, "atp-aaa111"), compose_project(prefix, "atp-bbb222")
    assert a != b
    assert a.startswith(prefix) and b.startswith(prefix)


def test_the_two_requirements_never_collide():
    """Same temp dir, different requirement — the projects must still differ, or a
    jupyter run and a strategy-runtime run would fight over one stack."""

    assert compose_project("atpsec003", "atp-x1") != compose_project("atpsec004", "atp-x1")


def test_no_integration_test_builds_its_own_project_name():
    """The class guard: enumerate the arms FROM THE SOURCE.

    Any integration test that passes ``-p`` to ``docker compose`` must get its
    project name from the shared builder. A third inline copy is exactly how this
    bug survived its own fix, and a checklist naming the two files we know about
    could not catch a file nobody added yet.
    """

    offenders = [
        path.name
        for path in sorted(_INTEGRATION_DIR.glob("test_*.py"))
        if _shells_compose_with_project(path.read_text(encoding="utf-8"))
        and "compose_project" not in path.read_text(encoding="utf-8")
    ]

    assert not offenders, (
        "these integration tests shell `docker compose -p` without the shared "
        "project-name builder, so each carries its own chance of drawing an "
        "invalid Docker reference:\n  " + "\n  ".join(offenders)
    )


def test_the_class_guard_can_actually_fail(tmp_path, monkeypatch):
    """A guard that scans a directory reports clean when it scans nothing.

    Point it at a planted offender and require it to notice — otherwise the
    check above is indistinguishable from one that found no files at all.
    """

    planted = tmp_path / "test_planted.py"
    planted.write_text('subprocess.run(["docker", "compose", "-p", "atpsec999x"])\n')
    innocent = tmp_path / "test_innocent.py"
    innocent.write_text('subprocess.run(["cargo", "test", "-p", "atp-data"])\n')

    offenders = [
        p.name
        for p in sorted(tmp_path.glob("test_*.py"))
        if _shells_compose_with_project(p.read_text()) and "compose_project" not in p.read_text()
    ]
    # Both directions: the planted offender is caught, the innocent `cargo -p` is not.
    assert offenders == ["test_planted.py"]


def test_the_real_scan_reaches_the_real_files():
    """Non-vacuity for the scan above: the directory must actually hold tests that
    shell Compose, or the guard passes by scanning an empty set."""

    with_compose = [
        p.name
        for p in _INTEGRATION_DIR.glob("test_*.py")
        if _shells_compose_with_project(p.read_text(encoding="utf-8"))
    ]
    assert len(with_compose) >= 2, (
        f"expected the two container-isolation tests to shell Compose, found {with_compose}"
    )
    assert re.match(r"test_", with_compose[0])
