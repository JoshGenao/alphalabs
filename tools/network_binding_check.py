#!/usr/bin/env python3
"""Network-binding evidence for SRS-SEC-002 (NFR-S3 / StRS SN-2.01).

Proves, at build time and with no live network, that the dashboard/API service
binds only to loopback / RFC 1918 addresses by default and that a
publicly-routable bind fails closed:

* **Default compose exposure is loopback/RFC 1918.** Every published port in
  ``docker-compose.yml`` is bound to a loopback / RFC 1918 host (or a
  ``${VAR:-<loopback>}`` interpolation default) — never a bare ``PORT:PORT``,
  which would publish on ``0.0.0.0`` (all interfaces).
* **No product source binds all interfaces.** No ``python/`` module carries a
  literal ``0.0.0.0`` / ``::`` bind default.
* **The bind policy fails closed on public/unspecified hosts.** The canonical
  ``is_allowed_bind_host`` / ``assert_bind_allowed`` allow only loopback + the
  three RFC 1918 ranges and raise ``BindPolicyError`` on ``0.0.0.0`` / ``::`` /
  link-local / CGNAT / any publicly-routable address.
* **Every declared default bind host is loopback.** ``runtime.start``,
  ``atp_dashboard.__main__``, and the ``runtime_services.json`` bind constants
  all default to ``127.0.0.1``.
* **External exposure is documented as auth-gated.** ``docs/DEPLOYMENT.md`` and
  ``SECURITY.md`` both state that reaching the dashboard from outside the local
  network requires an operator-managed authenticated access-control component.

The runtime intentionally provides NO public-bind opt-in; external exposure is
the operator's authenticated reverse proxy fronting the loopback bind (NFR-S3).

Mirrors the PASS/FAIL output style of ``tools/credential_security_check.py``.

Invoke:
    python3 tools/network_binding_check.py     # exit 0 on PASS, 1 on FAIL
"""

from __future__ import annotations

import argparse
import ast
import ipaddress
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
COMPOSE = ROOT / "docker-compose.yml"
CONTRACT = ROOT / "architecture" / "runtime_services.json"


class ContractCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise ContractCheckError(message)


def _import():
    if str(PYTHON_ROOT) not in sys.path:
        sys.path.insert(0, str(PYTHON_ROOT))
    from atp_runtime import assert_bind_allowed, is_allowed_bind_host
    from atp_runtime.errors import BindPolicyError

    return is_allowed_bind_host, assert_bind_allowed, BindPolicyError


# --------------------------------------------------------------------------- #
# docker-compose port parsing (text-level; no YAML dependency, no docker)
# --------------------------------------------------------------------------- #

# ``${VAR:-default}`` -> its default; ``${VAR}`` (no default) -> "" (no host).
_INTERP_DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}")
_INTERP_BARE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def _resolve_interpolations(value: str) -> str:
    value = _INTERP_DEFAULT.sub(lambda m: m.group(1), value)
    value = _INTERP_BARE.sub("", value)
    return value


def _published_port_entries(compose_text: str) -> list[str]:
    """Return every ``- "..."`` entry that appears under a ``ports:`` key."""

    entries: list[str] = []
    in_ports = False
    ports_indent = 0
    for line in compose_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if stripped == "ports:":
            in_ports = True
            ports_indent = indent
            continue
        if not in_ports:
            continue
        if stripped.startswith("- "):
            entries.append(stripped[2:].strip().strip('"').strip("'"))
            continue
        # A non-list key at or above the ports indentation ends the block.
        if indent <= ports_indent:
            in_ports = False
    return entries


def _mapping_bind_host(mapping: str) -> str | None:
    """The host interface a compose port mapping publishes on.

    Returns the host string, or ``None`` when the mapping has no host segment
    (``HOSTPORT:CONTAINERPORT`` / bare ``CONTAINERPORT``) — i.e. it publishes on
    ``0.0.0.0`` / all interfaces.
    """

    text = _resolve_interpolations(mapping).strip()
    if text.startswith("["):  # IPv6 host form: [addr]:hostport:containerport
        end = text.find("]")
        return text[1:end] if end != -1 else None
    parts = text.split(":")
    if len(parts) >= 3:
        return parts[0]
    return None


def check_compose_ports_loopback_bound(is_allowed) -> str:
    text = COMPOSE.read_text("utf-8")
    entries = _published_port_entries(text)
    if not entries:
        fail("no published port mappings found in docker-compose.yml (parser drift?)")
    for entry in entries:
        host = _mapping_bind_host(entry)
        if host is None:
            fail(
                f"docker-compose port mapping {entry!r} publishes on all interfaces "
                f"(0.0.0.0) — must bind loopback/RFC1918 (SRS-SEC-002)"
            )
        if not is_allowed(host):
            fail(
                f"docker-compose port mapping {entry!r} binds non-loopback/non-RFC1918 "
                f"host {host!r} (SRS-SEC-002)"
            )
    return f"all {len(entries)} docker-compose published ports bind loopback/RFC 1918 hosts"


def check_no_source_binds_all_interfaces() -> str:
    offenders: list[str] = []
    for path in sorted(PYTHON_ROOT.rglob("*.py")):
        if "test" in path.name:
            continue
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in ("0.0.0.0", "::"):
                    offenders.append(f"{path.relative_to(PYTHON_ROOT)}:{node.lineno}")
    if offenders:
        fail(
            "product source carries a literal all-interfaces bind (0.0.0.0 / ::) "
            f"(SRS-SEC-002): {offenders}"
        )
    return "no python/ product module carries a literal 0.0.0.0 / :: bind default"


def check_bind_policy_refuses_public(is_allowed, assert_allowed, BindPolicyError) -> str:
    allowed = (
        "127.0.0.1",
        "::1",
        "localhost",
        "10.0.0.5",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.10",
    )
    # Public, unspecified, link-local, CGNAT, and the ranges just outside RFC 1918.
    refused = (
        "0.0.0.0",
        "::",
        "8.8.8.8",
        "1.2.3.4",
        "169.254.1.1",
        "100.64.0.1",
        "172.15.0.1",
        "172.32.0.1",
        "not-an-ip",
    )
    for host in allowed:
        if not is_allowed(host):
            fail(f"bind host {host!r} should be ALLOWED (loopback/RFC 1918) — SRS-SEC-002")
        assert_allowed(host)  # must not raise
    for host in refused:
        if is_allowed(host):
            fail(f"bind host {host!r} should be REFUSED (SRS-SEC-002)")
        try:
            assert_allowed(host)
        except BindPolicyError:
            pass
        else:
            fail(f"assert_bind_allowed({host!r}) did not raise BindPolicyError")
    return (
        "bind policy allows loopback + RFC 1918 and fails closed on "
        "0.0.0.0 / :: / link-local / CGNAT / public"
    )


def _collect_bind_hosts(obj, path: str = "") -> list[tuple[str, object]]:
    found: list[tuple[str, object]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}/{key}" if path else key
            if "bind_host" in key.lower() and not isinstance(value, (dict, list)):
                found.append((here, value))
            else:
                found.extend(_collect_bind_hosts(value, here))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            found.extend(_collect_bind_hosts(value, f"{path}/{index}"))
    return found


def check_default_bind_host_is_loopback(is_allowed) -> str:
    import inspect

    from atp_runtime import OperatorInterfaceRuntime

    default_host = inspect.signature(OperatorInterfaceRuntime.start).parameters["host"].default
    if default_host != "127.0.0.1":
        fail(
            f"OperatorInterfaceRuntime.start default host is {default_host!r}; "
            f"must be loopback 127.0.0.1 (SRS-SEC-002)"
        )

    main_src = (PYTHON_ROOT / "atp_dashboard" / "__main__.py").read_text("utf-8")
    if 'os.environ.get("ATP_DASHBOARD_BIND_HOST", "127.0.0.1")' not in main_src:
        fail("atp_dashboard.__main__ must default ATP_DASHBOARD_BIND_HOST to loopback 127.0.0.1")

    contract = json.loads(CONTRACT.read_text("utf-8"))
    bind_hosts = _collect_bind_hosts(contract)
    if not bind_hosts:
        fail("no *bind_host constant found in runtime_services.json (contract drift?)")
    for key_path, value in bind_hosts:
        if not (
            isinstance(value, str) and is_allowed(value) and ipaddress.ip_address(value).is_loopback
        ):
            fail(
                f"runtime_services.json {key_path} = {value!r}; must be a loopback "
                f"default (SRS-SEC-002)"
            )
    return (
        f"default bind host is loopback in runtime.start, atp_dashboard.__main__, "
        f"and {len(bind_hosts)} runtime_services.json constants"
    )


def check_external_exposure_documented() -> str:
    for doc in ("docs/DEPLOYMENT.md", "SECURITY.md"):
        text = (ROOT / doc).read_text("utf-8").lower()
        if "srs-sec-002" not in text:
            fail(f"{doc} does not reference SRS-SEC-002")
        if "external" not in text or not ("authentication" in text or "authenticated" in text):
            fail(
                f"{doc} must state that external exposure requires operator-managed "
                f"authentication (SRS-SEC-002)"
            )
    return (
        "docs/DEPLOYMENT.md + SECURITY.md document that external exposure requires "
        "operator-managed authentication"
    )


def run_checks() -> list[str]:
    is_allowed, assert_allowed, BindPolicyError = _import()
    return [
        check_compose_ports_loopback_bound(is_allowed),
        check_no_source_binds_all_interfaces(),
        check_bind_policy_refuses_public(is_allowed, assert_allowed, BindPolicyError),
        check_default_bind_host_is_loopback(is_allowed),
        check_external_exposure_documented(),
    ]


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="SRS-SEC-002 network-binding evidence").parse_args(argv)
    try:
        evidence = run_checks()
    except ContractCheckError as error:
        print(f"NETWORK BINDING FAIL: {error}", file=sys.stderr)
        return 1

    print("NETWORK BINDING PASS — SRS-SEC-002 default-safe loopback/RFC 1918 bind")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
