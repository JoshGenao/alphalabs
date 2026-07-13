"""SRS-SEC-004 — L5 integration proof: ``docker inspect`` a real ``phase1-jupyter`` container.

Gated by ``ATP_RUN_INTEGRATION=1`` (see tests/conftest.py) and skipped when the Docker
CLI / Compose plugin is absent. This is the higher-fidelity, defense-in-depth companion
to the solo structural invariant in ``tests/domain/test_jupyter_credential_isolation.py``:
it creates the real ``phase1-jupyter`` container from ``docker-compose.yml`` and asserts
the Docker daemon applied the isolation SRS-SEC-004 (SyRS NFR-S6) requires —

* the SRS-SEC-001 credential vault is NOT mounted, and every catalogued brokerage /
  notification secret resolves to an empty value in the container environment (no
  credential reaches the kernel),
* the shared SSD/NAS data tiers are mounted READ-ONLY (read-only market-data /
  backtest-result access), and
* the container is NOT on host networking and every network it joins is ``Internal``
  (no gateway → no host / LAN / internet egress → no execution-API path).

The absence of an execution-API *peer* on Jupyter's network is proven statically (the
domain test + ``jupyter_isolation_check``): the execution engine and IB Gateway declare
no ``networks:`` and so join the default bridge, which Jupyter is off. At runtime a
single-service create leaves Jupyter's internal network with only Jupyter on it.

It is **not** part of the solo gate (``-m "not integration and not e2e"``) and does not
gate the ``passes`` flip; it is the operator's real-container verification path. The
concrete operator-supplied JupyterLab image is deferred (IF-13 / SRS-RES-001), so the
compose template remains the declarative source of truth this test exercises.
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
SERVICE = "phase1-jupyter"

# The catalogued brokerage / notification / vault secrets that must resolve empty in
# the Jupyter container (mirrors x-atp-no-secrets / deployment_check._SECRET_BLANK_KEYS).
_SECRET_KEYS = (
    "ATP_IB_ACCOUNT",
    "ATP_SMTP_API_KEY",
    "ATP_SMS_API_KEY",
    "DATABENTO_API_KEY",
    "SHARADAR_API_KEY",
    "ATP_VAULT_FILE",
    "ATP_VAULT_KEY_FILE",
    "ATP_VAULT_PASSPHRASE",
)


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
def created_jupyter_container():
    """Create (build, do not start) the Jupyter container and yield its inspect JSON."""

    if not _docker_available():
        pytest.skip("docker CLI / compose plugin not available")

    data_root = Path(tempfile.mkdtemp(prefix="atp-sec004-"))
    ssd = data_root / "ssd"
    nas = data_root / "nas"
    ssd.mkdir()
    nas.mkdir()
    project = f"atpsec004{data_root.name.replace('-', '')[:20]}"

    env = {
        **os.environ,
        "ATP_SSD_DATA_DIR": str(ssd),
        "ATP_NAS_DATA_DIR": str(nas),
        # Populate the catalogued secrets in the environment to PROVE the blanking
        # merge wins — the container env must still resolve them empty.
        "ATP_IB_ACCOUNT": "real-account-should-be-blanked",
        "ATP_SMTP_API_KEY": "smtp-should-be-blanked",
        "ATP_SMS_API_KEY": "sms-should-be-blanked",
        "DATABENTO_API_KEY": "databento-should-be-blanked",
        "SHARADAR_API_KEY": "sharadar-should-be-blanked",
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
        assert cid, "no container id for the Jupyter service after create"

        inspect = subprocess.run(
            ["docker", "inspect", cid[0]],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert inspect.returncode == 0, inspect.stderr
        container = json.loads(inspect.stdout)[0]

        # Resolve each attached network's Internal flag (no gateway → no egress).
        network_names = list((container.get("NetworkSettings") or {}).get("Networks") or {})
        internal_flags: dict[str, bool] = {}
        for name in network_names:
            net = subprocess.run(
                ["docker", "network", "inspect", name],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if net.returncode == 0:
                internal_flags[name] = bool(json.loads(net.stdout)[0].get("Internal"))
        container["_atp_network_internal_flags"] = internal_flags
        yield container
    finally:
        _run(["down", "-v"], timeout=300)
        shutil.rmtree(data_root, ignore_errors=True)


def test_jupyter_does_not_mount_credential_vault(created_jupyter_container) -> None:
    mounts = created_jupyter_container.get("Mounts") or []
    for mount in mounts:
        assert mount.get("Destination") != "/run/atp-secrets", (
            "Jupyter must not mount the SRS-SEC-001 credential vault (SRS-SEC-004)"
        )


def test_jupyter_environment_blanks_every_secret(created_jupyter_container) -> None:
    raw_env = (created_jupyter_container.get("Config") or {}).get("Env") or []
    env: dict[str, str] = {}
    for item in raw_env:
        key, _, value = item.partition("=")
        env[key] = value
    for key in _SECRET_KEYS:
        assert env.get(key, "") == "", (
            f"Jupyter env must blank {key} (got {env.get(key)!r}); a populated host value "
            f"must not leak into the kernel (SRS-SEC-004)"
        )


def test_jupyter_data_tiers_are_read_only(created_jupyter_container) -> None:
    mounts = created_jupyter_container.get("Mounts") or []
    tier_mounts = [m for m in mounts if m.get("Destination") in ("/ssd", "/nas")]
    assert tier_mounts, "Jupyter must mount the SSD/NAS data tiers"
    for mount in tier_mounts:
        assert mount.get("RW") is False, (
            f"data tier {mount.get('Destination')} must be mounted read-only (SRS-SEC-004)"
        )


def test_jupyter_has_no_host_network(created_jupyter_container) -> None:
    network_mode = created_jupyter_container["HostConfig"]["NetworkMode"]
    assert network_mode != "host", "Jupyter must not use host networking (SRS-SEC-004)"


def test_jupyter_networks_are_internal(created_jupyter_container) -> None:
    """Every network Jupyter joins is internal (no gateway → no execution-API egress)."""

    internal_flags = created_jupyter_container["_atp_network_internal_flags"]
    assert internal_flags, "Jupyter must attach to at least one inspected network"
    assert "host" not in internal_flags and "bridge" not in internal_flags, (
        "Jupyter must not be on the default/host bridge (SRS-SEC-004)"
    )
    for name, is_internal in internal_flags.items():
        assert is_internal, f"Jupyter network {name!r} must be internal (no egress)"
