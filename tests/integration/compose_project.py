"""The one Compose project-name builder the container-isolation tests share.

Compose builds image references as ``<project>-<service>``, so the project name
has to be legal in a Docker reference on its own AND after that suffix is
appended.

WHY THIS IS A SHARED MODULE. The original expression,
``f"atpsec004{data_root.name.replace('-', '')[:20]}"``, strips hyphens but not
underscores — and ``tempfile.mkdtemp`` draws its random suffix from
``[a-z0-9_]``. When a draw ended in ``_`` (or the 20-character truncation landed
on one) the image became ``...ntkxt33_-phase1-jupyter``, which the daemon
rejects with ``invalid reference format``. It failed only on unlucky draws,
which is why it passed for months and then turned CI red on an unrelated commit.

That was fixed IN THE JUPYTER TEST ONLY. Its sibling
``test_strategy_container_inspect.py`` carried a byte-identical copy of the bug
and turned `main` red the same way on 2026-09-02 — ``atpsec003...9kn9veb_``.
A fix applied to one call site is not applied to the class, so the builder now
lives here and both import it. `tests/unit/test_compose_project_name.py` fails
if a third one appears.

Keeping ONLY ``[a-z0-9]`` removes the whole class rather than the character that
happened to bite: no separator can reach either end, and no future mkdtemp
alphabet change can reintroduce it.
"""

from __future__ import annotations

import re

#: What the daemon accepts for one path component of an image reference.
DOCKER_REF = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")


def compose_project(prefix: str, dir_name: str) -> str:
    """A Compose project name Docker will accept, derived from a temp dir name.

    `prefix` identifies the owning requirement (``atpsec003`` / ``atpsec004``) so
    two concurrent runs never share a project and tear down each other's
    containers.
    """

    suffix = re.sub(r"[^a-z0-9]", "", dir_name.lower())[:20]
    return f"{prefix}{suffix}"
