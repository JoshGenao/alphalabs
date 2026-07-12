"""SRS-SEC-003 — L5 integration proof: ``docker inspect`` a real strategy container.

Gated by ``ATP_RUN_INTEGRATION=1`` (see tests/conftest.py) and skipped when the
Docker CLI / Compose plugin is absent. This is the higher-fidelity, defense-in-depth
companion to the solo structural invariant in
``tests/domain/test_strategy_container_least_privilege.py``: it creates the real
``phase1-strategy-runtime`` container from ``docker-compose.yml`` and asserts the
Docker daemon applied the least-privilege HostConfig the SRS-SEC-003 acceptance
requires — no privileged mode, no host network, and read-only shared data tiers.

It is **not** part of the solo gate (``-m "not integration and not e2e"``) and does
not gate the ``passes`` flip; it is the operator's real-container verification path.
The concrete Docker-backed StrategyContainerRuntime is deferred, so the compose
template remains the declarative source of truth this test exercises.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
SERVICE = "phase1-strategy-runtime"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                timeout=30,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.fixture()
def created_strategy_container():
    """Create (build, do not start) the strategy container and yield its inspect JSON."""

    if not _docker_available():
        pytest.skip("docker CLI / compose plugin not available")

    data_root = Path(tempfile.mkdtemp(prefix="atp-sec003-"))
    ssd = data_root / "ssd"
    nas = data_root / "nas"
    ssd.mkdir()
    nas.mkdir()
    project = f"atpsec003{data_root.name.replace('-', '')[:20]}"

    env = {
        **os.environ,
        "ATP_SSD_DATA_DIR": str(ssd),
        "ATP_NAS_DATA_DIR": str(nas),
    }
    base = ["docker", "compose", "-p", project, "--profile", "phase1"]

    def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            base + args,
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    try:
        created = _run(["create", "--build", SERVICE], timeout=1800)
        if created.returncode != 0:
            pytest.fail(f"docker compose create failed:\n{created.stderr}")

        cid = _run(["ps", "-aq", SERVICE], timeout=60).stdout.strip().splitlines()
        assert cid, "no container id for the strategy service after create"

        inspect = subprocess.run(
            ["docker", "inspect", cid[0]],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert inspect.returncode == 0, inspect.stderr
        yield json.loads(inspect.stdout)[0]
    finally:
        _run(["down", "-v"], timeout=300)
        shutil.rmtree(data_root, ignore_errors=True)


def test_strategy_container_is_not_privileged(created_strategy_container) -> None:
    host_config = created_strategy_container["HostConfig"]
    assert host_config["Privileged"] is False, "strategy container must not be privileged"
    assert "ALL" in (host_config.get("CapDrop") or []), "strategy container must drop ALL capabilities"
    security_opt = host_config.get("SecurityOpt") or []
    assert any("no-new-privileges" in opt for opt in security_opt), (
        "strategy container must set no-new-privileges"
    )


def test_strategy_container_has_no_host_network(created_strategy_container) -> None:
    network_mode = created_strategy_container["HostConfig"]["NetworkMode"]
    assert network_mode != "host", "strategy container must not use host networking"


def test_strategy_container_data_tiers_are_read_only(created_strategy_container) -> None:
    mounts = created_strategy_container.get("Mounts") or []
    tier_mounts = [m for m in mounts if m.get("Destination") in ("/ssd", "/nas")]
    assert tier_mounts, "strategy container must mount the SSD/NAS data tiers"
    for mount in tier_mounts:
        assert mount.get("RW") is False, (
            f"data tier {mount.get('Destination')} must be mounted read-only (SRS-SEC-003)"
        )
