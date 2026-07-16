#!/usr/bin/env python3
"""Jupyter credential + execution-API isolation evidence for SRS-SEC-004 (SyRS NFR-S6).

Proves, by static inspection of ``docker-compose.yml`` (no Docker daemon, no live
container), that the embedded Jupyter research environment (``phase1-jupyter``) is
isolated from live trading credentials and the execution APIs — the SRS-SEC-004
acceptance clauses, refined by SyRS NFR-S6:

* **No brokerage / notification credentials.** Jupyter's ``environment`` merges the
  ``x-atp-no-secrets`` anchor **first** (YAML merge is earlier-wins), which blanks
  every catalogued secret + all vault-unlock material, so even a populated ``.env``
  cannot leak a credential into the kernel. No catalogued secret is re-set inline,
  and the SRS-SEC-001 credential vault (``/run/atp-secrets``) is not mounted at all,
  so Jupyter cannot read a brokerage credential.
* **No direct access to the execution engine / no live orders.** Jupyter is confined
  to a dedicated network declared ``internal: true`` (no gateway → no host / LAN /
  internet egress) on which no execution-API peer (the execution engine, the IB
  Gateway) is placed — so it can open no socket to a brokerage / execution API. A
  container with no explicit network would join the default Compose bridge, which
  both routes outbound through the host AND reaches every other default-bridge
  container; requiring a dedicated internal network with no execution peer removes
  both. Host / shared-namespace networking is refused for the same reason.
* **Read-only market-data / backtest-result access.** Jupyter mounts ONLY the
  sanctioned SSD/NAS data tiers, and only read-only — it reads market data and
  backtest results through the data layer (filesystem, no network) and can write
  into no shared tier.

The credential-blanking + no-vault half is also asserted repo-wide by
``deployment_check.assert_credential_vault_wiring`` (jupyter / strategy / IB
Gateway); this check additionally proves the merge ORDER, the read-only-only data
allow-list, and the no-execution-network invariant, and fails closed on the
Compose-equivalent bypass syntaxes a naive substring check would miss.

The concrete dashboard->Jupyter proxy attach and the operator-supplied JupyterLab
image are deferred (IF-13 / SRS-RES-001); until they land ``docker-compose.yml`` is
the declarative template and this static inspection is the authoritative SRS-SEC-004
"Security test" evidence — the same convention SRS-ARCH-004 / SRS-SEC-003 are
verified under. A gated ``tests/integration`` test additionally runs ``docker
inspect`` on a real ``phase1-jupyter`` container when ``ATP_RUN_INTEGRATION=1``.

Forbidden-token scans run against the *comment-stripped* compose text: directive
tokens (``/run/atp-secrets`` etc.) legitimately appear in explanatory ``#`` comments,
and only the effective configuration must be judged.

Reuses the hardened text parser of ``tools/container_isolation_check.py`` and the
credential helpers of ``tools/deployment_check.py`` (no YAML dependency).

Invoke:
    python3 tools/jupyter_isolation_check.py                         # exit 0 PASS, 1 FAIL
    python3 tools/jupyter_isolation_check.py --fixture on-default-bridge  # must FAIL
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from container_isolation_check import (
    _child_indent,
    _flow_items,
    _parse_volume_entry,
    _scalar_bool,
    _service_key_count,
    _service_scalar,
    _strip_comments,
    _volume_mounts,
)
from deployment_check import (
    _SECRET_BLANK_KEYS,
    _anchor_block,
    _service_block,
    _service_has_vault_mount,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"

# Security-relevant service keys that must appear at most once on the Jupyter
# service — a duplicate silently overrides the first value (Docker last-wins), so a
# duplicate is a bypass and is refused outright.
_SINGLETON_SECURITY_KEYS = (
    "privileged",
    "network_mode",
    "pid",
    "ipc",
    "user",
    "userns_mode",
    "cap_add",
    "security_opt",
    "networks",
    "volumes",
    "environment",
)

# Keys on which a YAML alias / ``${VAR}`` interpolation is refused (its resolved
# value is invisible to a static check). ``environment`` is deliberately excluded:
# it resolves ``*atp-no-secrets`` / ``*atp-env`` by design and is validated by the
# credential-env assertion below, not blanket-refused.
_ALIAS_REFUSED_KEYS = (
    "privileged",
    "network_mode",
    "pid",
    "ipc",
    "user",
    "userns_mode",
    "cap_add",
    "security_opt",
    "networks",
    "volumes",
)


class JupyterIsolationCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise JupyterIsolationCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def jupyter_isolation_contract(config: dict) -> dict:
    if "jupyter_isolation_contract" not in config:
        fail("architecture metadata is missing the jupyter_isolation_contract block")
    return config["jupyter_isolation_contract"]


# --------------------------------------------------------------------------- #
# credential-env (anchor merge) helpers
# --------------------------------------------------------------------------- #


def _anchor_label(anchor_block: str) -> str | None:
    """Return the anchor label defined via ``&`` on the anchor's first line."""

    first = anchor_block.splitlines()[0] if anchor_block else ""
    match = re.search(r"&([A-Za-z0-9_-]+)", first)
    return match.group(1) if match else None


def _environment_lines(eff_block: str) -> list[str] | None:
    """Return the lines nested under ``environment:`` at the service-child indent.

    Operates on the comment-stripped block; returns None when the service declares
    no ``environment:`` key so the caller can fail closed (a Jupyter service with no
    credential-blanking merge is a violation).
    """

    child = _child_indent(eff_block)
    env_indent: int | None = None
    collected: list[str] = []
    for line in eff_block.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if env_indent is None:
            if indent == child and line.strip().startswith("environment:"):
                env_indent = indent
            continue
        if indent <= env_indent:
            break
        collected.append(line)
    return collected if env_indent is not None else None


def _merge_alias_order(env_lines: list[str]) -> list[str] | None:
    """Ordered anchor labels in the ``<<:`` merge under ``environment``.

    Returns the labels (``*`` stripped) in declared order — the FIRST wins under
    YAML merge semantics. Returns None when there is no ``<<:`` merge, and an empty
    list when a merge is present but its value cannot be parsed (caller fails closed).
    """

    for line in env_lines:
        stripped = line.strip()
        if stripped.startswith("<<:"):
            value = stripped[len("<<:") :].strip()
            if value.startswith("["):
                return [item.lstrip("*").strip() for item in _flow_items(value)]
            if value.startswith("*"):
                return [value[1:].strip()]
            return []
    return None


def _inline_env_keys(env_lines: list[str]) -> dict[str, str]:
    """Explicit ``KEY: value`` pairs directly under ``environment`` (not the merge).

    Explicit keys override a merged mapping regardless of order, so an inline
    ``ATP_IB_ACCOUNT: real`` re-introduces a credential the blanking merge removed.
    """

    indents = [len(ln) - len(ln.lstrip()) for ln in env_lines if ln.strip()]
    if not indents:
        return {}
    env_child = min(indents)
    out: dict[str, str] = {}
    for line in env_lines:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent != env_child:
            continue
        stripped = line.strip()
        if stripped.startswith("<<:") or stripped.startswith("- "):
            continue
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _service_networks(eff_block: str) -> list[str] | None:
    """Network names a service attaches to, in BOTH Compose spellings.

    Compose accepts a ``networks:`` as a list (``- name`` / inline ``[a, b]``) OR a
    mapping (``name:`` / ``name: {}`` / ``name:\\n  aliases: …``). A list-only reader
    returns an empty list for the mapping form, which would make a peer attached via
    map syntax read as unattached — a fail-open. This reads both. Returns None when
    there is no ``networks:`` key so the caller can distinguish "absent" from "empty".
    """

    child = _child_indent(eff_block)
    key_indent: int | None = None
    body: list[str] = []
    for line in eff_block.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if key_indent is None:
            stripped = line.strip()
            if indent == child and stripped == "networks:":
                key_indent = indent
                continue
            if indent == child and stripped.startswith("networks:"):
                inline = stripped[len("networks:") :].strip()
                if inline.startswith("["):
                    return _flow_items(inline)
                # An inline scalar / alias (`networks: *x`) — return it verbatim so
                # the caller fails closed on the alias.
                return [inline] if inline else []
            continue
        if indent <= key_indent:
            break
        body.append(line)
    if key_indent is None:
        return None
    if not body:
        return []
    item_indent = min(len(ln) - len(ln.lstrip()) for ln in body)
    names: list[str] = []
    for line in body:
        if (len(line) - len(line.lstrip())) != item_indent:
            continue  # a deeper line (e.g. `aliases:`) belongs to a map entry
        stripped = line.strip()
        if stripped.startswith("- "):
            names.append(stripped[2:].strip().strip('"').strip("'"))
        else:
            token = stripped.split(":", 1)[0].strip().strip('"').strip("'")
            if token:
                names.append(token)
    return names


# --------------------------------------------------------------------------- #
# per-service isolation assertions
# --------------------------------------------------------------------------- #


def _assert_no_unresolvable_constructs(eff_block: str, service: str) -> None:
    """Fail closed on Compose constructs Docker WOULD apply but a static check can't
    resolve, on Jupyter's isolation surface: a service-level ``<<:`` merge or
    ``extends:`` (could inject an unchecked credential env / execution network /
    vault mount), and a YAML alias / ``${VAR}`` on a security key. Mirrors the
    SRS-SEC-003 / SRS-SEC-002 fail-closed-on-interpolation stance. The template's
    only merge is ``environment: <<: [*...]`` — nested under ``environment`` (neither
    a service-level nor a refused security key), so it is unaffected and is validated
    by the credential-env assertion instead.
    """

    child = _child_indent(eff_block)
    for line in eff_block.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent != child:
            continue
        stripped = line.strip()
        if stripped.startswith("<<:"):
            fail(
                f"{service} uses a service-level `<<:` merge; SRS-SEC-004 refuses a merge that "
                f"could inject an unchecked credential env / execution network / mount from an anchor"
            )
        if stripped.startswith("extends:"):
            fail(
                f"{service} uses `extends:`; SRS-SEC-004 refuses inheritance that could bring in an "
                f"unchecked credential env, execution network, or vault mount from another service"
            )
        for key in _ALIAS_REFUSED_KEYS:
            if stripped.startswith(f"{key}:"):
                value = stripped[len(key) + 1 :].strip()
                if "*" in value or "$" in value:
                    fail(
                        f"{service} sets `{key}:` to a YAML alias / interpolation ({value!r}); "
                        f"SRS-SEC-004 refuses a security value it cannot resolve statically"
                    )


def _assert_no_duplicate_security_keys(eff_block: str, service: str) -> None:
    for key in _SINGLETON_SECURITY_KEYS:
        count = _service_key_count(eff_block, key)
        if count > 1:
            fail(
                f"{service} declares `{key}:` {count} times; a later value silently overrides the "
                f"first (Docker last-wins), so duplicate security keys are refused (SRS-SEC-004)"
            )


def _assert_credential_env(contract: dict, compose_text: str, eff_block: str, service: str) -> None:
    """No brokerage / notification credential can reach the Jupyter kernel.

    Requires the credential-blanking anchor be merged FIRST (blanks win), that the
    anchor blanks every catalogued secret + vault-unlock key, and that no catalogued
    secret is re-set inline (which would override the merge).
    """

    env_lines = _environment_lines(eff_block)
    if env_lines is None:
        fail(
            f"{service} declares no `environment:` block; SRS-SEC-004 requires it merge the "
            f"credential-blanking anchor so no brokerage credential reaches Jupyter"
        )

    no_secrets_key = contract["no_secrets_anchor"]
    anchor_block = _anchor_block(compose_text, no_secrets_key)
    if anchor_block is None:
        fail(f"compose is missing the {no_secrets_key} credential-blanking anchor (SRS-SEC-004)")
    label = _anchor_label(anchor_block)
    if not label:
        fail(f"{no_secrets_key} defines no YAML anchor (`&label`) to merge (SRS-SEC-004)")

    # The anchor must blank EVERY catalogued secret + all vault-unlock material, so
    # an isolated service cannot receive a credential even via .env.
    eff_anchor = _strip_comments(anchor_block)
    for key in _SECRET_BLANK_KEYS:
        if f'{key}: ""' not in eff_anchor:
            fail(
                f"{no_secrets_key} does not blank {key} (SRS-SEC-004 credential isolation); the "
                f'blanking anchor must set every catalogued secret to ""'
            )

    order = _merge_alias_order(env_lines)
    if order is None:
        fail(
            f"{service} environment does not merge a credential-blanking anchor via `<<:`; "
            f"SRS-SEC-004 requires the catalogued secrets be blanked for Jupyter"
        )
    if label not in order:
        fail(
            f"{service} environment does not merge {label!r} (the credential-blanking anchor); "
            f"SRS-SEC-004 requires the catalogued secrets be blanked for Jupyter"
        )
    if order[0] != label:
        fail(
            f"{service} merges {label!r} at position {order.index(label)}, not first (order: "
            f"{order}); YAML merge is earlier-wins, so the blanks only win when {label!r} is "
            f"listed FIRST (SRS-SEC-004)"
        )

    # Explicit inline keys override a merged mapping regardless of order — an inline
    # catalogued secret re-introduces a credential the blanking merge removed.
    inline = _inline_env_keys(env_lines)
    for key in _SECRET_BLANK_KEYS:
        if inline.get(key, "") != "":
            fail(
                f"{service} sets {key}={inline[key]!r} inline under environment, overriding the "
                f"blanking merge; SRS-SEC-004 forbids any brokerage credential reaching Jupyter"
            )


def _assert_no_credential_access(contract: dict, eff_block: str, service: str) -> None:
    """Jupyter mounts no credential vault, docker socket, or another container's mounts."""

    for token in contract["forbidden_mount_tokens"]:
        if token in eff_block:
            fail(f"{service} must not reference {token!r} (SRS-SEC-004 filesystem isolation)")
    if _service_has_vault_mount(eff_block):
        fail(
            f"{service} must not mount the SRS-SEC-001 credential vault (SRS-SEC-004 credential "
            f"isolation); Jupyter cannot be able to read a brokerage credential"
        )
    if contract["credential_vault_mount_token"] in eff_block:
        fail(
            f"{service} references the credential-vault path "
            f"{contract['credential_vault_mount_token']!r}; SRS-SEC-004 forbids the vault reaching Jupyter"
        )


def _assert_read_only_data(contract: dict, eff_block: str, service: str) -> None:
    """Jupyter mounts EXACTLY the sanctioned read-only data tiers — nothing else.

    Read-only market-data / backtest-result access is the positive SRS-SEC-004 clause;
    a read-write tier, a host bind, or an extra shared volume is rejected (strict
    allow-list). Volumes are normalized across short / long / flow Compose spellings.
    """

    mounts = _volume_mounts(eff_block)
    if not mounts:
        fail(
            f"{service} declares no volumes; SRS-SEC-004 requires read-only market-data / "
            f"backtest-result mounts (via the data layer)"
        )
    allowed: dict[tuple[str, str], bool] = {}
    for sanctioned in contract["sanctioned_readonly_data_volumes"]:
        parsed = _parse_volume_entry(f"- {sanctioned}")
        allowed[(parsed["source"], parsed["target"])] = parsed["read_only"]
    for mount in mounts:
        if mount["kind"] == "bind" or mount["source"][:1] in ("/", ".", "~", "{", "$"):
            fail(
                f"{service} uses a host/bind mount ({mount['raw']!r}); Jupyter may mount only the "
                f"sanctioned read-only named data tiers (SRS-SEC-004)"
            )
        if (mount["source"], mount["target"]) not in allowed:
            fail(
                f"{service} mounts {mount['raw']!r}, which is not a sanctioned read-only data tier; "
                f"SRS-SEC-004 grants Jupyter read-only market-data / backtest-result access only"
            )
        if not mount["read_only"]:
            fail(
                f"{service} mounts data tier {mount['raw']!r} read-write; SRS-SEC-004 requires "
                f"read-only access to market data and backtest results"
            )
    present = {(m["source"], m["target"]) for m in mounts if m["read_only"]}
    for source, target in allowed:
        if (source, target) not in present:
            fail(f"{service} must mount the read-only data tier {source}:{target} (SRS-SEC-004)")


def _assert_no_host_network(eff_block: str, service: str) -> None:
    """No host / shared-namespace networking — it would bypass the internal-network
    isolation and let Jupyter reach the broker via the host's ports."""

    network_mode = _service_scalar(eff_block, "network_mode")
    if network_mode is not None and (
        network_mode == "host"
        or network_mode.startswith("service:")
        or network_mode.startswith("container:")
    ):
        fail(
            f"{service} must not declare `network_mode: {network_mode}`; host / shared-namespace "
            f"networking would bypass the internal-network isolation and reach the broker (SRS-SEC-004)"
        )
    if _service_scalar(eff_block, "pid") == "host":
        fail(f"{service} must not share the host PID namespace (SRS-SEC-004)")
    if _service_scalar(eff_block, "ipc") in ("host", "shareable"):
        fail(f"{service} must not share the host IPC namespace (SRS-SEC-004)")


def _assert_no_execution_network(
    contract: dict, compose_text: str, eff_block: str, service: str
) -> None:
    """Jupyter has NO direct network path to the execution engine / IB Gateway.

    It must attach to a dedicated ``internal: true`` network (no gateway → no
    egress), never the default bridge, and share NO network with any execution-API
    peer — so it can open no socket to a brokerage / execution API and submit no
    live order.
    """

    if not contract.get("require_internal_network"):
        return

    networks = _service_networks(eff_block)
    if not networks:
        fail(
            f"{service} declares no networks, so it joins the default Compose bridge and shares it "
            f"with the execution engine / IB Gateway; SRS-SEC-004 requires a dedicated internal "
            f"network with no execution-API peer"
        )
    if "default" in networks:
        fail(
            f"{service} attaches to the `default` bridge, which the execution engine / IB Gateway "
            f"also join; SRS-SEC-004 forbids sharing a network with an execution-API peer"
        )

    jupyter_nets = set(networks)
    for net in networks:
        net_block = _service_block(compose_text, net)
        if net_block is None:
            fail(
                f"{service} is attached to network {net!r}, which is not declared at the top level; "
                f"SRS-SEC-004 requires an internal, no-egress network"
            )
        eff_net = _strip_comments(net_block)
        if _scalar_bool(_service_scalar(eff_net, "external")) is True:
            fail(
                f"{service} network {net!r} is `external: true`; its membership cannot be proven "
                f"internal / execution-free, so SRS-SEC-004 refuses it (fail closed)"
            )
        # Read the network's DIRECT-child `internal:` scalar — not a substring of the
        # whole block, so a nested `labels: {internal: true}` under an `internal:
        # false` network cannot masquerade as internal.
        internal = _service_scalar(eff_net, "internal")
        if _scalar_bool(internal) is not True:
            fail(
                f"{service} is attached to network {net!r} whose `internal:` is {internal!r}, not "
                f"true; SRS-SEC-004 requires a no-egress (internal) network so Jupyter reaches no "
                f"host / LAN / broker"
            )

    # A forbidden execution-API peer must share NO network with Jupyter. A peer with
    # no explicit `networks:` is implicitly on the `default` bridge.
    for peer in contract.get("forbidden_network_peers", []):
        peer_block = _service_block(compose_text, peer)
        if peer_block is None:
            continue
        eff_peer = _strip_comments(peer_block)

        # A peer that shares Jupyter's network NAMESPACE via `network_mode:
        # service:<jupyter>` / `container:<...>` gets a direct localhost path to
        # Jupyter (and Jupyter to it) WITHOUT declaring a shared network, so the
        # `networks:` comparison below would miss it. Fail closed. (Jupyter's own
        # side is refused by `_assert_no_host_network`; this closes the peer side.)
        peer_mode = _service_scalar(eff_peer, "network_mode")
        if peer_mode is not None:
            if "*" in peer_mode or "$" in peer_mode:
                fail(
                    f"{peer} sets `network_mode: {peer_mode}` via an alias / interpolation; "
                    f"SRS-SEC-004 cannot prove it does not share {service}'s network namespace "
                    f"(fail closed)"
                )
            if peer_mode == f"service:{service}" or peer_mode.startswith("container:"):
                fail(
                    f"{peer} shares a network namespace via `network_mode: {peer_mode}`; SRS-SEC-004 "
                    f"forbids an execution-API peer sharing {service}'s network namespace (a direct "
                    f"localhost path to the execution engine / IB Gateway)"
                )

        # Read the peer's `networks:` in BOTH list and map spellings — a map-style
        # attachment (`networks:\n  atp_research_net: {}`) must not read as "on
        # default". Fail closed on an alias / interpolation we cannot resolve.
        peer_nets = _service_networks(eff_peer)
        for name in peer_nets or []:
            if "*" in name or "$" in name:
                fail(
                    f"{peer} attaches to network {name!r} via an alias / interpolation; SRS-SEC-004 "
                    f"cannot prove it is not {service}'s network (fail closed)"
                )
        peer_net_set = set(peer_nets) if peer_nets else {"default"}
        shared = jupyter_nets & peer_net_set
        if shared:
            fail(
                f"{service} shares network(s) {sorted(shared)} with {peer!r} (an execution-API peer); "
                f"SRS-SEC-004 forbids any direct network path from Jupyter to the execution engine / "
                f"IB Gateway"
            )


def _assert_no_published_ports(contract: dict, eff_block: str, service: str) -> None:
    """IF-13 / SRS-RES-001: the research environment publishes NO host port.

    It is "not a standalone external endpoint" — reached ONLY through the
    dashboard's same-origin ``/research/`` proxy. (A publish would be dead
    anyway on an ``internal: true`` network, but a dead publish invites a
    future "fix" that widens the surface; refuse it statically.)
    """

    if not contract.get("subject_must_not_publish_ports"):
        return
    if _service_key_count(eff_block, "ports") > 0:
        fail(
            f"{service} publishes ports; IF-13 / SRS-RES-001 mandates the research environment is "
            f"reached only through the dashboard's same-origin proxy, never a standalone endpoint"
        )


def _assert_internal_only_networks(
    compose_text: str, networks: list[str], service: str, requirement: str
) -> None:
    """Every attached network must be declared, non-external, ``internal: true``."""

    for net in networks:
        net_block = _service_block(compose_text, net)
        if net_block is None:
            fail(
                f"{service} is attached to network {net!r}, which is not declared at the top "
                f"level; {requirement} requires an internal, no-egress network"
            )
        eff_net = _strip_comments(net_block)
        if _scalar_bool(_service_scalar(eff_net, "external")) is True:
            fail(
                f"{service} network {net!r} is `external: true`; its membership cannot be proven "
                f"internal / execution-free, so {requirement} refuses it (fail closed)"
            )
        internal = _service_scalar(eff_net, "internal")
        if _scalar_bool(internal) is not True:
            fail(
                f"{service} is attached to network {net!r} whose `internal:` is {internal!r}, not "
                f"true; {requirement} requires a no-egress (internal) network"
            )


def _assert_research_proxy_isolation(contract: dict, compose_text: str) -> list[str]:
    """The one-way dashboard->Jupyter hop (SRS-RES-001) must widen NO surface.

    ``phase1-research-proxy`` is the only service on BOTH ``atp_research_net``
    and the dashboard-facing edge network, so it is Jupyter's entire reachable
    world — its pivot surface must be strictly smaller than Jupyter's own:
    no secrets (blanking anchor merged first), NO volumes at all, no vault /
    docker socket / ``volumes_from``, no host / shared-namespace networking,
    no published ports, only declared ``internal: true`` networks (never the
    default bridge), and NO network shared with an execution peer
    (``research_proxy_forbidden_network_peers``). ``phase1-dashboard-api`` is
    deliberately NOT in that peer list — sharing the edge network with it IS
    the design; the one-way property comes from the hop's fixed upstream
    (python/atp_research_proxy) plus dashboard-api never joining
    ``atp_research_net`` (asserted independently above).
    """

    service = contract.get("research_proxy_service")
    if not service:
        return []
    raw = _service_block(compose_text, service)
    if raw is None:
        fail(
            f"compose is missing the one-way research proxy {service!r} "
            f"(SRS-RES-001 / SRS-SEC-004 dashboard->Jupyter hop)"
        )
    eff = _strip_comments(raw)

    _assert_no_unresolvable_constructs(eff, service)
    _assert_no_duplicate_security_keys(eff, service)
    _assert_credential_env(contract, compose_text, eff, service)
    _assert_no_credential_access(contract, eff, service)
    _assert_no_host_network(eff, service)
    # Strictly less privilege than Jupyter itself: the L4 hop needs NO
    # filesystem, so ANY volume is a widening and is refused.
    if _service_key_count(eff, "volumes") > 0:
        fail(
            f"{service} declares volumes; the one-way research proxy needs no filesystem access "
            f"(strictly less privilege than Jupyter) — SRS-SEC-004 pivot-surface rule"
        )
    if _service_key_count(eff, "ports") > 0:
        fail(
            f"{service} publishes ports; the research proxy is reachable only on its internal "
            f"networks (IF-13 / SRS-SEC-004)"
        )

    networks = _service_networks(eff)
    if not networks:
        fail(
            f"{service} declares no networks, so it joins the default Compose bridge (shared with "
            f"the execution engine / IB Gateway); the research proxy may join only dedicated "
            f"internal networks (SRS-SEC-004)"
        )
    if "default" in networks:
        fail(
            f"{service} attaches to the `default` bridge, which the execution engine / IB Gateway "
            f"also join; SRS-SEC-004 forbids the research proxy sharing a network with an "
            f"execution-API peer"
        )
    _assert_internal_only_networks(compose_text, networks, service, "SRS-SEC-004")

    proxy_nets = set(networks)
    for peer in contract.get("research_proxy_forbidden_network_peers", []):
        peer_block = _service_block(compose_text, peer)
        if peer_block is None:
            continue
        eff_peer = _strip_comments(peer_block)
        peer_mode = _service_scalar(eff_peer, "network_mode")
        if peer_mode is not None:
            if "*" in peer_mode or "$" in peer_mode:
                fail(
                    f"{peer} sets `network_mode: {peer_mode}` via an alias / interpolation; "
                    f"SRS-SEC-004 cannot prove it does not share {service}'s network namespace "
                    f"(fail closed)"
                )
            if peer_mode == f"service:{service}" or peer_mode.startswith("container:"):
                fail(
                    f"{peer} shares a network namespace via `network_mode: {peer_mode}`; "
                    f"SRS-SEC-004 forbids an execution-API peer sharing the research proxy's "
                    f"network namespace"
                )
        peer_nets = _service_networks(eff_peer)
        for name in peer_nets or []:
            if "*" in name or "$" in name:
                fail(
                    f"{peer} attaches to network {name!r} via an alias / interpolation; "
                    f"SRS-SEC-004 cannot prove it is not the research proxy's network (fail closed)"
                )
        peer_net_set = set(peer_nets) if peer_nets else {"default"}
        shared = proxy_nets & peer_net_set
        if shared:
            fail(
                f"{service} shares network(s) {sorted(shared)} with {peer!r} (an execution-API "
                f"peer); SRS-SEC-004 forbids any network path from the research hop to the "
                f"execution engine / IB Gateway"
            )

    return [
        f"{service}: one-way dashboard->Jupyter hop — no secrets (blanking anchor merged first), "
        f"no volumes, no published ports, internal-only networks, and no network shared with an "
        f"execution-API peer (the fixed-upstream forwarder is Jupyter's entire reachable world)",
    ]


def _assert_service_isolation(contract: dict, compose_text: str, service: str) -> list[str]:
    raw = _service_block(compose_text, service)
    if raw is None:
        fail(f"compose is missing the Jupyter service {service!r} (SRS-SEC-004)")
    eff = _strip_comments(raw)

    _assert_no_unresolvable_constructs(eff, service)
    _assert_no_duplicate_security_keys(eff, service)
    _assert_credential_env(contract, compose_text, eff, service)
    _assert_no_credential_access(contract, eff, service)
    _assert_read_only_data(contract, eff, service)
    _assert_no_host_network(eff, service)
    _assert_no_execution_network(contract, compose_text, eff, service)
    _assert_no_published_ports(contract, eff, service)

    return [
        f"{service}: brokerage/notification credentials blanked (blanking anchor merged first, no "
        f"vault mount, no inline secret), only read-only named data tiers (market data + backtest "
        f"results), confined to an internal network with no execution-engine / IB-Gateway peer "
        f"(no live-order path), and no published ports (IF-13: reached only through the "
        f"dashboard's same-origin proxy)",
    ]


def _assert_security_doc(contract: dict, root: Path) -> list[str]:
    doc_rel = contract["security_doc"]
    doc_path = root / doc_rel
    if not doc_path.exists():
        fail(f"security doc is missing: {doc_rel}")
    text = doc_path.read_text(encoding="utf-8")
    marker = contract["security_doc_marker"]
    if marker not in text:
        fail(f"{doc_rel} does not document {marker} (Jupyter credential / execution isolation)")
    low = text.lower()
    if "jupyter" not in low:
        fail(f"{doc_rel} does not describe Jupyter isolation (SRS-SEC-004)")
    if "isolat" not in low:
        fail(f"{doc_rel} does not describe Jupyter isolation (SRS-SEC-004)")
    if "read-only" not in low and "read only" not in low:
        fail(f"{doc_rel} does not state Jupyter's read-only data access (SRS-SEC-004)")
    return [f"{doc_rel} documents {marker} Jupyter credential + execution-API isolation"]


def assert_jupyter_isolation_static(config: dict, root: Path = ROOT) -> list[str]:
    contract = jupyter_isolation_contract(config)
    compose_path = root / contract["compose_file"]
    if not compose_path.exists():
        fail(f"compose file is missing: {contract['compose_file']}")
    compose_text = compose_path.read_text(encoding="utf-8")

    subject_services = contract["subject_services"]
    if not subject_services:
        fail("jupyter_isolation_contract names no subject_services (SRS-SEC-004 would be vacuous)")

    evidence: list[str] = ["SRS-SEC-004 Jupyter credential + execution-API isolation evidence:"]
    for service in subject_services:
        evidence.extend(_assert_service_isolation(contract, compose_text, service))
    evidence.extend(_assert_research_proxy_isolation(contract, compose_text))
    evidence.extend(_assert_security_doc(contract, root))
    return evidence


# --------------------------------------------------------------------------- #
# negative self-test fixtures (prove the check FAILS on a violation)
# --------------------------------------------------------------------------- #

_FIXTURES = (
    "on-default-bridge",
    "research-net-not-internal",
    "shares-execution-network",
    "peer-shares-namespace",
    "dashboard-shares-network",
    "peer-map-network",
    "external-research-net",
    "vault-mounted",
    "secret-not-blanked",
    "merge-order-reversed",
    "inline-secret-override",
    "service-level-merge",
    "extends-inheritance",
    "aliased-networks",
    "duplicate-networks",
    "host-network",
    "docker-socket",
    "writable-data-tier",
    "extra-shared-volume",
    "jupyter-publishes-ports",
    "research-proxy-on-default",
    "research-proxy-shares-execution-network",
    "research-proxy-vault-mounted",
    "research-proxy-merge-order-reversed",
    "dashboard-on-research-net-via-edge-rename",
)


def make_fixture_root(fixture: str) -> tempfile.TemporaryDirectory[str]:
    temp_dir = tempfile.TemporaryDirectory()
    temp_root = Path(temp_dir.name)
    (temp_root / "architecture").mkdir()

    shutil.copy2(CONFIG_PATH, temp_root / "architecture" / "runtime_services.json")
    shutil.copy2(ROOT / "docker-compose.yml", temp_root / "docker-compose.yml")
    shutil.copy2(ROOT / "SECURITY.md", temp_root / "SECURITY.md")

    compose_path = temp_root / "docker-compose.yml"
    text = compose_path.read_text(encoding="utf-8")

    if fixture == "on-default-bridge":
        # Drop Jupyter's internal-network attachment: it falls back to the default
        # Compose bridge, shared with the execution engine / IB Gateway. Rejected.
        text = text.replace("    networks:\n      - atp_research_net\n", "")
    elif fixture == "research-net-not-internal":
        # The research network loses its no-egress guarantee. Rejected.
        text = text.replace("    internal: true", "    internal: false")
    elif fixture == "shares-execution-network":
        # Put the execution engine onto Jupyter's network — a direct execution-API
        # path. Rejected (forbidden peer shares a network).
        text = text.replace(
            "        ATP_CRATE: atp-execution\n    env_file:",
            "        ATP_CRATE: atp-execution\n    networks:\n      - atp_research_net\n    env_file:",
        )
    elif fixture == "peer-shares-namespace":
        # The execution engine joins Jupyter's network NAMESPACE directly (a
        # localhost path) without declaring a shared network. Rejected.
        text = text.replace(
            "        ATP_CRATE: atp-execution\n    env_file:",
            "        ATP_CRATE: atp-execution\n    network_mode: service:phase1-jupyter\n    env_file:",
        )
    elif fixture == "dashboard-shares-network":
        # The dashboard/API service (SRS-API-001 live-control REST: kill switch,
        # live designation, Hot-Swap) joins Jupyter's network — a path to
        # live-control APIs. Rejected (dashboard-api is a forbidden peer).
        text = text.replace(
            "    dockerfile: docker/dashboard-api.Dockerfile\n",
            "    dockerfile: docker/dashboard-api.Dockerfile\n    networks:\n      - atp_research_net\n",
        )
    elif fixture == "peer-map-network":
        # A forbidden peer attaches to Jupyter's network via Compose MAP syntax
        # (`networks:\n  atp_research_net: {}`), which a list-only parser misses.
        text = text.replace(
            "        ATP_CRATE: atp-execution\n    env_file:",
            "        ATP_CRATE: atp-execution\n    networks:\n      atp_research_net: {}\n    env_file:",
        )
    elif fixture == "external-research-net":
        # An external network's membership cannot be proven internal / execution-free.
        text = text.replace(
            "  atp_research_net:\n    driver: bridge\n    internal: true",
            "  atp_research_net:\n    external: true",
        )
    elif fixture == "vault-mounted":
        # Mount the credential vault into Jupyter — it could read a brokerage
        # credential. Rejected.
        text = text.replace(
            "      - atp_nas:/nas:ro\n",
            "      - atp_nas:/nas:ro\n      - ${ATP_SECRETS_DIR:-./secrets}:/run/atp-secrets:ro\n",
        )
    elif fixture == "secret-not-blanked":
        # The blanking anchor no longer blanks a catalogued brokerage credential.
        text = text.replace('  ATP_IB_ACCOUNT: ""', '  ATP_IB_ACCOUNT: "real-account"')
    elif fixture == "merge-order-reversed":
        # `*atp-env` (placeholder secrets) now wins the merge over the blanks
        # (YAML merge is earlier-wins). Rejected.
        text = text.replace(
            "<<: [*atp-no-secrets, *atp-env]",
            "<<: [*atp-env, *atp-no-secrets]",
        )
    elif fixture == "inline-secret-override":
        # An explicit inline secret overrides the blanking merge. Rejected.
        text = text.replace(
            "      <<: [*atp-no-secrets, *atp-env]\n",
            "      <<: [*atp-no-secrets, *atp-env]\n      ATP_IB_ACCOUNT: real-account\n",
        )
    elif fixture == "service-level-merge":
        # A service-level `<<:` merge could inject credential env / networks / mounts
        # from an anchor. Refused outright.
        text = text.replace(
            "    environment:\n      <<: [*atp-no-secrets, *atp-env]",
            "    <<: *atp-env\n    environment:\n      <<: [*atp-no-secrets, *atp-env]",
        )
    elif fixture == "extends-inheritance":
        # `extends:` could inherit a credential env / execution network / mount from
        # another service. Refused outright.
        text = text.replace(
            "    dockerfile: docker/jupyter.Dockerfile\n",
            "    dockerfile: docker/jupyter.Dockerfile\n    extends:\n      service: phase1-ib-gateway\n",
        )
    elif fixture == "aliased-networks":
        # A YAML alias on a security key hides its resolved value from the check.
        text = text.replace(
            "    networks:\n      - atp_research_net\n",
            "    networks: *atp-env\n",
        )
    elif fixture == "duplicate-networks":
        # A duplicate `networks:` whose later value (Docker last-wins) could reattach
        # Jupyter elsewhere. Refused outright.
        text = text.replace(
            "    networks:\n      - atp_research_net\n",
            "    networks:\n      - atp_research_net\n    networks:\n      - atp_strategy_net\n",
        )
    elif fixture == "host-network":
        # Host networking bypasses the internal-network isolation. Rejected.
        text = text.replace(
            "    dockerfile: docker/jupyter.Dockerfile\n",
            "    dockerfile: docker/jupyter.Dockerfile\n    network_mode: host\n",
        )
    elif fixture == "docker-socket":
        # Mounting the host Docker socket would let Jupyter reach any container /
        # credential. Rejected.
        text = text.replace(
            "      - atp_nas:/nas:ro\n",
            "      - atp_nas:/nas:ro\n      - /var/run/docker.sock:/var/run/docker.sock:ro\n",
        )
    elif fixture == "writable-data-tier":
        # A read-write data tier — Jupyter's access must be read-only. Rejected.
        text = text.replace("atp_ssd:/ssd:ro", "atp_ssd:/ssd")
    elif fixture == "extra-shared-volume":
        # An extra non-sanctioned named volume outside the read-only data tiers.
        text = text.replace(
            "      - atp_nas:/nas:ro\n",
            "      - atp_nas:/nas:ro\n      - research_shared:/shared\n",
        )
    elif fixture == "jupyter-publishes-ports":
        # Jupyter grows a published host port again — IF-13 forbids a standalone
        # endpoint (the research environment is reached only through the
        # dashboard's same-origin proxy). Rejected. (Anchored on Jupyter's
        # networks list + trailing comment, unique to the jupyter service.)
        text = text.replace(
            "    networks:\n      - atp_research_net\n    # NO published ports",
            '    networks:\n      - atp_research_net\n    ports:\n'
            '      - "127.0.0.1:8888:8888"\n    # NO published ports',
        )
    elif fixture == "research-proxy-on-default":
        # The research proxy loses its explicit networks and falls back to the
        # default bridge — shared with the execution engine / IB Gateway (a
        # 2-hop pivot from Jupyter). Rejected.
        text = text.replace(
            "    networks:\n      - atp_research_net\n      - atp_research_edge_net\n",
            "",
        )
    elif fixture == "research-proxy-shares-execution-network":
        # The execution engine joins the research proxy's edge network — a
        # network path from the research hop to the execution API. Rejected.
        text = text.replace(
            "        ATP_CRATE: atp-execution\n    env_file:",
            "        ATP_CRATE: atp-execution\n    networks:\n"
            "      - default\n      - atp_research_edge_net\n    env_file:",
        )
    elif fixture == "research-proxy-vault-mounted":
        # The research proxy gains the credential-vault volumes anchor — a
        # widening of Jupyter's entire reachable world. Rejected (no volumes
        # at all are allowed on the hop).
        text = text.replace(
            '    command: ["python", "-m", "atp_research_proxy"]\n',
            '    command: ["python", "-m", "atp_research_proxy"]\n    volumes: *atp-volumes\n',
        )
    elif fixture == "research-proxy-merge-order-reversed":
        # The proxy's env merge order flips: placeholder secrets win over the
        # blanks. Rejected. (Anchored on the proxy's unique inline env key so
        # only the research-proxy service is mutated, not Jupyter.)
        text = text.replace(
            '      <<: [*atp-no-secrets, *atp-env]\n      ATP_RESEARCH_PROXY_PORT:',
            '      <<: [*atp-env, *atp-no-secrets]\n      ATP_RESEARCH_PROXY_PORT:',
        )
    elif fixture == "dashboard-on-research-net-via-edge-rename":
        # dashboard-api's edge attachment is "simplified" to atp_research_net —
        # putting the live-control REST on Jupyter's network. The ORIGINAL
        # SRS-SEC-004 forbidden-peer assertion must still bite in the new
        # topology. (Anchored on dashboard-api's unique default+edge pair.)
        text = text.replace(
            "      - default\n      - atp_research_edge_net",
            "      - default\n      - atp_research_net",
        )
    else:
        temp_dir.cleanup()
        raise ValueError(f"unknown fixture: {fixture}")

    compose_path.write_text(text, encoding="utf-8")
    return temp_dir


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        choices=list(_FIXTURES),
        help="Run the check against a temporary workspace containing a known violation.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    root = ROOT
    if args.fixture:
        temp_dir = make_fixture_root(args.fixture)
        root = Path(temp_dir.name)

    try:
        config = load_config(root)
        evidence = assert_jupyter_isolation_static(config, root)
    except JupyterIsolationCheckError as error:
        print(f"SRS-SEC-004 FAIL: {error}", file=sys.stderr)
        return 1
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    print("SRS-SEC-004 PASS — Jupyter isolated from live credentials and execution APIs")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
