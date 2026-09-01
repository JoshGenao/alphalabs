"""L7 domain — the IF-10 alert path agrees with itself across three artifacts.

SRS-NOTIF-001 / SYS-46. `REQUIRED_CHANNELS` is Email AND Push enforced
fail-closed, so a disagreement about WHERE the email relay lives does not
degrade the email channel — it silences BOTH, including the push channel that is
proven to a real locked iPhone. That makes this a safety-path invariant rather
than a deployment detail.

The endpoint is spelled out in three places that no compiler relates:

  * `crates/atp-adapters/src/notification/smtp.rs` — DEFAULT_RELAY_HOST / _PORT,
    what the adapter dials when nothing overrides it;
  * `docker-compose.yml` — the service name that DNS resolves, and whether the
    deployment declares the relay at all;
  * `docker/notification-egress-entrypoint.sh` — the port Postfix actually
    listens on.

Change any one and the other two keep passing their own tests. The failure is
silent and total: every alert fails `Unconfigured` or `TransportUnavailable`
while the relay sits healthy on a port nobody dials.

These tests are written against the SHIPPED artifacts, never a fixture copy —
docs/playbooks/adversarial-precheck.md rule 7, "implemented is not shipped".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SMTP_RS = ROOT / "crates" / "atp-adapters" / "src" / "notification" / "smtp.rs"
COMPOSE = ROOT / "docker-compose.yml"
ENTRYPOINT = ROOT / "docker" / "notification-egress-entrypoint.sh"
DOCKERFILE = ROOT / "docker" / "notification-egress.Dockerfile"
MANIFEST = ROOT / "architecture" / "runtime_services.json"


def _rust_const(name: str) -> str:
    """Read one `const NAME: T = <value>;` out of smtp.rs.

    Anchored on the const DECLARATION rather than on a bare token: the port
    number 1025 appears many times in that file's tests, and a token-scoped match
    would silently read one of those instead (docs/playbooks/test-integrity.md
    rule 27).
    """

    match = re.search(
        rf"^const {re.escape(name)}: [^=]+= *(?P<value>[^;]+);",
        SMTP_RS.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match is not None, f"smtp.rs no longer declares `const {name}` — the contract moved"
    return match.group("value").strip().strip('"')


def test_the_adapter_dials_the_service_name_compose_actually_declares() -> None:
    host = _rust_const("DEFAULT_RELAY_HOST")
    compose_text = COMPOSE.read_text(encoding="utf-8")
    assert re.search(rf"^  {re.escape(host)}:$", compose_text, re.MULTILINE), (
        f"smtp.rs dials {host!r} by default, but docker-compose.yml declares no "
        "service by that name. DNS would not resolve it and every email alert "
        "would fail TransportUnavailable — taking the push channel down with it, "
        "because REQUIRED_CHANNELS is fail-closed."
    )


def test_the_adapter_dials_the_port_the_relay_listens_on() -> None:
    port = _rust_const("DEFAULT_RELAY_PORT")
    entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
    assert f'RELAY_PORT="${{ATP_SMTP_RELAY_PORT:-{port}}}"' in entrypoint, (
        f"smtp.rs dials port {port} by default, but the relay entrypoint defaults "
        "its listener to a different one. Both artifacts are internally consistent "
        "and nothing connects."
    )
    assert f"EXPOSE {port}" in DOCKERFILE.read_text(encoding="utf-8"), (
        f"the Dockerfile documents a port other than {port}, which is what the adapter dials"
    )


def test_the_relay_is_a_required_service_not_an_optional_one() -> None:
    """A deployment without the relay must fail the gate, not start quietly.

    `phase1-ntfy` is deliberately optional — the operator may already run one.
    This one cannot be: with the email channel unconfigured, `REQUIRED_CHANNELS`
    fail-closed means NOTHING sends on either channel.
    """

    deployment = json.loads(MANIFEST.read_text(encoding="utf-8"))["deployment"]
    host = _rust_const("DEFAULT_RELAY_HOST")
    assert host in deployment["required_services"], (
        f"{host} is not in deployment.required_services, so deployment_check.py "
        "would pass a stack that can send no operator alert at all."
    )
    assert "docker/notification-egress.Dockerfile" in deployment["required_dockerfiles"]


def test_the_relay_credential_is_the_one_the_adapter_authenticates_with() -> None:
    """The relay's inbound password and the adapter's key must be one variable.

    They were separate strings once. A relay seeded from its own variable
    authenticates nobody, and the failure arrives as a bare 535 with nothing in
    it to say the two sides disagree about which secret they share.
    """

    entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
    assert 'RELAY_PASSWORD="${ATP_SMTP_API_KEY:-}"' in entrypoint
    # And the username fallback must mirror smtp.rs, which falls back to the
    # sender when ATP_SMTP_RELAY_USER is unset.
    assert 'RELAY_USER="${ATP_SMTP_RELAY_USER:-${ATP_SMTP_SENDER:-}}"' in entrypoint
    smtp_rs = SMTP_RS.read_text(encoding="utf-8")
    assert 'read("ATP_SMTP_RELAY_USER").unwrap_or_else(|| sender.clone())' in smtp_rs, (
        "smtp.rs changed how it derives the relay username; the entrypoint's "
        "fallback now disagrees with it"
    )
