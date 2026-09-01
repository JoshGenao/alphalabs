#!/usr/bin/env python3
"""Phase 1 Docker Compose deployment checks for SRS-ARCH-004."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class DeploymentCheckError(AssertionError):
    pass


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def fail(message: str) -> None:
    raise DeploymentCheckError(message)


def deployment_config(config: dict) -> dict:
    if "deployment" not in config:
        fail("architecture metadata is missing deployment block")
    return config["deployment"]


def _service_block_contains(compose_text: str, service_name: str, needle: str) -> bool:
    pattern = re.compile(
        rf"^\s+{re.escape(service_name)}:\n(?P<body>(?:^[ \t].*\n|^\s*\n)*)",
        re.MULTILINE,
    )
    match = pattern.search(compose_text)
    if match is None:
        return False
    return needle in match.group("body")


def assert_compose_services(config: dict, root: Path = ROOT) -> list[str]:
    deployment = deployment_config(config)
    compose_path = root / deployment["compose_file"]
    if not compose_path.exists():
        fail(f"compose file is missing: {deployment['compose_file']}")

    compose_text = compose_path.read_text(encoding="utf-8")

    profile = deployment["phase1_profile"]
    if f'"{profile}"' not in compose_text and f"'{profile}'" not in compose_text:
        fail(f"compose file does not declare the {profile!r} profile")

    missing = [
        service
        for service in deployment["required_services"]
        if not re.search(rf"^\s+{re.escape(service)}:\s*$", compose_text, re.MULTILINE)
    ]
    if missing:
        fail(f"compose file is missing Phase 1 services: {', '.join(missing)}")

    bind_host = deployment["dashboard_bind_host"]
    if not _service_block_contains(compose_text, "phase1-dashboard-api", f"{bind_host}:"):
        fail(f"phase1-dashboard-api must publish ports bound to {bind_host} (SRS-SEC-002)")

    return [
        f"compose declares {len(deployment['required_services'])} Phase 1 services "
        f"under profile {profile!r}",
        f"phase1-dashboard-api binds {bind_host} (SRS-SEC-002)",
    ]


def assert_compose_env_and_volumes(config: dict, root: Path = ROOT) -> list[str]:
    deployment = deployment_config(config)
    compose_text = (root / deployment["compose_file"]).read_text(encoding="utf-8")

    missing_env = [
        env_var for env_var in deployment["required_env_vars"] if env_var not in compose_text
    ]
    if missing_env:
        fail("compose file does not reference required env vars: " + ", ".join(missing_env))

    for volume in deployment["required_volumes"]:
        env_token = "${" + volume["env"]
        if env_token not in compose_text:
            fail(f"compose file does not bind {volume['name']} volume to {volume['env']}")
        if not re.search(rf"^\s+{re.escape(volume['name'])}:\s*$", compose_text, re.MULTILINE):
            fail(f"compose file does not declare named volume {volume['name']}")

    return [
        f"compose passes {len(deployment['required_env_vars'])} env vars to Phase 1 services",
        "SSD primary tier and NAS archive tier mounted via ATP_SSD_DATA_DIR / ATP_NAS_DATA_DIR",
    ]


def assert_dockerfiles_present(config: dict, root: Path = ROOT) -> list[str]:
    deployment = deployment_config(config)
    missing = [
        dockerfile
        for dockerfile in deployment["required_dockerfiles"]
        if not (root / dockerfile).exists()
    ]
    if missing:
        fail("required Dockerfiles are missing: " + ", ".join(missing))
    # DERIVED, not restated. The hardcoded name list this replaces said
    # "(core-runtime, strategy-python, dashboard-api, jupyter, ib-gateway)" while
    # the count beside it read 6 the moment a sixth Dockerfile was registered —
    # an evidence string that outruns its own subject
    # (docs/playbooks/adversarial-precheck.md rule 12).
    names = ", ".join(
        Path(dockerfile).name.removesuffix(".Dockerfile")
        for dockerfile in deployment["required_dockerfiles"]
    )
    return [f"{len(deployment['required_dockerfiles'])} Dockerfiles present ({names})"]


def assert_env_example(config: dict, root: Path = ROOT) -> list[str]:
    deployment = deployment_config(config)
    env_path = root / deployment["env_example"]
    if not env_path.exists():
        fail(f"env template is missing: {deployment['env_example']}")
    env_text = env_path.read_text(encoding="utf-8")
    missing = [
        env_var
        for env_var in deployment["required_env_vars"]
        if not re.search(rf"^{re.escape(env_var)}=", env_text, re.MULTILINE)
    ]
    if missing:
        fail(f"{deployment['env_example']} does not list required keys: " + ", ".join(missing))
    return [
        f"{deployment['env_example']} enumerates all "
        f"{len(deployment['required_env_vars'])} required env vars"
    ]


def assert_deployment_doc(config: dict, root: Path = ROOT) -> list[str]:
    deployment = deployment_config(config)
    doc_path = root / deployment["deployment_doc"]
    if not doc_path.exists():
        fail(f"deployment doc is missing: {deployment['deployment_doc']}")
    doc_text = doc_path.read_text(encoding="utf-8")
    missing = [
        keyword
        for keyword in deployment["portability_doc_keywords"]
        if keyword.lower() not in doc_text.lower()
    ]
    if missing:
        fail(
            f"{deployment['deployment_doc']} does not address portability keywords: "
            + ", ".join(missing)
        )
    return [
        f"{deployment['deployment_doc']} documents Phase 1 target, "
        "cloud VPS as future target, and portability constraints"
    ]


# Phase 1 services that load ATP catalogued credentials (run config/readiness)
# and therefore MUST be able to open the vault.
_VAULT_CONSUMER_SERVICES = (
    "phase1-orchestrator",
    "phase1-execution-engine",
    "phase1-strategy-engine",
    "phase1-simulation-engine",
    "phase1-market-data",
    "phase1-data-layer",
    "phase1-factor-pipeline",
    "phase1-notification-dispatcher",
    "phase1-dashboard-api",
)
# Services that must NOT open the vault: jupyter + strategy containers
# (SRS-SEC-004 least-privilege) and the IB Gateway (out-of-band auth).
_VAULT_ISOLATED_SERVICES = (
    "phase1-jupyter",
    "phase1-strategy-runtime",
    "phase1-ib-gateway",
)
_MOUNT_TOKEN = "/run/atp-secrets"
# Keys the x-atp-no-secrets anchor must blank for isolated services: the six
# catalogued secrets + every vault-unlock secret (key file AND passphrase).
# ATP_PUSH_TOPIC is in this list because on ntfy the topic IS a credential —
# whoever holds it can publish an alert to the operator's phone.
_SECRET_BLANK_KEYS = (
    "ATP_IB_ACCOUNT",
    "ATP_SMTP_API_KEY",
    "ATP_PUSH_TOPIC",
    "ATP_PUSH_TOKEN",
    "DATABENTO_API_KEY",
    "SHARADAR_API_KEY",
    "ATP_VAULT_FILE",
    "ATP_VAULT_KEY_FILE",
    "ATP_VAULT_PASSPHRASE",
)


def _anchor_block(compose_text: str, name: str) -> str | None:
    """Return the text of a top-level ``x-...`` anchor block, or None if absent."""

    marker = f"\n{name}:"
    start = compose_text.find(marker)
    if start < 0:
        return None
    rest = compose_text[start + 1 :]
    end = re.search(r"\n[A-Za-z0-9_-]+:", rest[len(name) + 1 :])
    return rest if end is None else rest[: end.start() + len(name) + 1]


def _service_block(compose_text: str, name: str) -> str | None:
    """Return the compose text of one phase1 service block, or None if absent."""

    marker = f"\n  {name}:\n"
    start = compose_text.find(marker)
    if start < 0:
        return None
    rest = compose_text[start + len(marker) :]
    # The block ends at the next 2-space service key OR the next top-level
    # (0-indent) section (`volumes:` / `networks:`), whichever comes first — so
    # the LAST service does not over-capture the trailing named-volume block.
    end = re.search(r"\n  [A-Za-z0-9_-]+:\n|\n[A-Za-z0-9_-]+:\n", rest)
    return rest if end is None else rest[: end.start()]


def _service_has_vault_mount(block: str) -> bool:
    """True if the service block actually mounts the vault (anchor ref or literal).

    Matches the ``*atp-volumes`` anchor reference (which carries the mount) or an
    inlined ``:/run/atp-secrets:ro`` bind — NOT the bare ``/run/atp-secrets``
    string that appears in explanatory comments.
    """

    return "*atp-volumes" in block or ":/run/atp-secrets:ro" in block


def assert_credential_vault_wiring(config: dict, root: Path = ROOT) -> list[str]:
    """The SRS-SEC-001 encrypted vault must be deliverable to every credential consumer.

    Verifies the compose stack passes ``ATP_VAULT_FILE`` / ``ATP_VAULT_KEY_FILE``,
    that each credential-consuming phase1 service mounts the vault volume
    (``*atp-volumes`` includes the read-only ``/run/atp-secrets`` bind), and that
    the isolated services (jupyter + strategy containers + IB Gateway) do NOT
    receive the mount (SRS-SEC-004 / least-privilege).
    """

    deployment = deployment_config(config)
    compose_text = (root / deployment["compose_file"]).read_text(encoding="utf-8")

    for token in ("ATP_VAULT_FILE", "ATP_VAULT_KEY_FILE"):
        if token not in compose_text:
            fail(f"compose does not pass {token} to services (SRS-SEC-001 vault delivery)")
    if f"{_MOUNT_TOKEN}:ro" not in compose_text:
        fail(
            f"compose does not mount the credential vault read-only at {_MOUNT_TOKEN} (SRS-SEC-001)"
        )

    # The *atp-no-secrets anchor must blank EVERY catalogued secret + all
    # vault-unlock material (key file AND passphrase) so an isolated service
    # cannot receive a credential even via .env (SRS-SEC-004).
    no_secrets = _anchor_block(compose_text, "x-atp-no-secrets")
    if no_secrets is None:
        fail("compose is missing the x-atp-no-secrets blanking anchor (SRS-SEC-004)")
    for key in _SECRET_BLANK_KEYS:
        if f'{key}: ""' not in no_secrets:
            fail(f"x-atp-no-secrets does not blank {key} (SRS-SEC-004 credential isolation)")

    # The vault mount lives inside the *atp-volumes anchor; a consuming service
    # references it via `volumes: *atp-volumes` and keeps the full credential env.
    for service in _VAULT_CONSUMER_SERVICES:
        block = _service_block(compose_text, service)
        if block is None:
            fail(f"compose is missing credential-consuming service {service}")
        if not _service_has_vault_mount(block):
            fail(
                f"{service} consumes credentials but does not mount the vault (volumes: *atp-volumes)"
            )
        if "*atp-no-secrets" in block:
            fail(f"{service} consumes credentials but blanks them via *atp-no-secrets")

    # Isolated services must neither mount the vault NOR receive the catalogued
    # secrets — they merge *atp-no-secrets over *atp-env to blank every secret.
    for service in _VAULT_ISOLATED_SERVICES:
        block = _service_block(compose_text, service)
        if block is None:
            continue
        if _service_has_vault_mount(block):
            fail(f"{service} must NOT mount the credential vault (SRS-SEC-004 / least-privilege)")
        if "*atp-no-secrets" not in block:
            fail(
                f"{service} must blank the catalogued secrets via *atp-no-secrets "
                "(SRS-SEC-004 credential isolation)"
            )

    return [
        f"compose delivers the SRS-SEC-001 vault + credentials to all "
        f"{len(_VAULT_CONSUMER_SERVICES)} credential-consuming services and blanks both from "
        f"{len(_VAULT_ISOLATED_SERVICES)} isolated services (jupyter / strategy / IB Gateway)",
    ]


def assert_ntfy_upstream_well_formed(_config: dict, root: Path = ROOT) -> list[str]:
    """Reject a malformed ATP_NTFY_UPSTREAM before ntfy is ever started.

    This knob is the ONLY thing that gets an alert to a locked iPhone (see
    docs/DEPLOYMENT.md), and ntfy gives no signal about it whatsoever: an empty
    value, a valid URL and `not-a-url` all produce byte-identical startup logs,
    verified against 2.27.0. So a typo here does not fail — it silently downgrades
    the push channel to foreground-only, while ATP keeps publishing 200s and
    recording deliveries. SYS-46 would report success for a page the operator
    never received.

    Nothing else can catch it. The key cannot live in the ARCH-005 catalogue —
    ``merge_env`` treats an empty value as "not provided", so a key whose normal
    state is empty always fails readiness as "not set" — so ``atp_config`` never
    sees it. This is the preflight.

    EMPTY IS VALID and means disabled: an Android target needs no upstream.
    """

    from urllib.parse import urlparse

    def from_env_file() -> str | None:
        env_path = root / ".env"
        if not env_path.is_file():
            return None
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("ATP_NTFY_UPSTREAM="):
                return stripped.partition("=")[2].strip().strip('"').strip("'")
        return None

    exported = os.environ.get("ATP_NTFY_UPSTREAM")
    in_file = from_env_file()

    # THE STALE-SHELL TRAP, caught mechanically rather than described.
    # `set -a; . ./.env; set +a` exports the file into the shell, and the shell
    # WINS over `--env-file` — compose resolves `${VAR:-default}` from the
    # environment first. So editing `.env` afterwards changes nothing until the
    # operator re-sources, and the stale value survives any number of
    # `up -d --force-recreate` runs with no error.
    #
    # Only the HARMFUL direction is refused: an exported EMPTY value silently
    # overriding a configured one, which disables the iOS wake-up while `.env`
    # says it is on. An export to a different non-empty URL is a deliberate
    # one-off override and is left alone.
    if exported == "" and in_file:
        fail(
            "ATP_NTFY_UPSTREAM is exported EMPTY in this shell while .env sets it "
            f"to {in_file!r}. The shell wins over --env-file, so ntfy would start "
            "with the iOS wake-up DISABLED and a locked iPhone would receive "
            "nothing, silently. Re-source .env (`set -a; . ./.env; set +a`) or "
            "open a new shell."
        )

    raw = exported if exported is not None else in_file
    source = "the environment" if exported is not None else ".env"
    if raw is None or raw == "":
        return ["ATP_NTFY_UPSTREAM is unset or empty (iOS wake-up disabled; valid)"]

    # Everything below is deliberately strict, because `urlparse` validates
    # almost nothing: it happily returns netloc="exa mple.com", netloc='nt"fy.sh'
    # and netloc=":8080" (no host at all). A scheme-and-netloc check therefore
    # blesses exactly the typos this gate exists to catch.
    def refuse(why: str) -> None:
        fail(
            f"ATP_NTFY_UPSTREAM in {source} {why} — ntfy accepts a bad upstream "
            f"SILENTLY, so this would leave a locked iPhone receiving nothing "
            f"while ATP kept recording deliveries"
        )

    if any(c.isspace() or ord(c) < 0x20 for c in raw):
        refuse("contains whitespace or a control character")

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        refuse(f"must be an http(s) URL, got scheme {parsed.scheme or '(none)'!r}")
    # It is a BASE url: a path, query or fragment means the operator pasted a
    # topic or a deep link rather than the server root.
    if parsed.path not in ("", "/"):
        refuse(f"must be a base URL with no path, got path {parsed.path!r}")
    if parsed.query or parsed.fragment:
        refuse("must be a base URL with no query string or fragment")

    try:
        port = parsed.port
    except ValueError:
        refuse("has a malformed port")
        port = None
    if port is not None and not (1 <= port <= 65535):
        refuse(f"has a port outside 1-65535: {port}")

    # `parsed.hostname` STRIPS userinfo, so `https://user:pass@ntfy.sh` would
    # otherwise sail through with host "ntfy.sh" — putting a credential in a
    # config value that is not catalogued secret and gets echoed in evidence.
    if parsed.username is not None or parsed.password is not None:
        refuse("must not embed userinfo (user:pass@)")

    host = parsed.hostname  # None for ":8080"; strips IPv6 brackets
    if not host:
        refuse("has no host")
    elif not all(c.isalnum() or c in "-._:" for c in host):
        refuse(f"has a host with illegal characters: {host!r}")

    evidence = [f"ATP_NTFY_UPSTREAM from {source} is a well-formed {parsed.scheme} base URL"]

    # A NOTE, deliberately not a refusal. Only ntfy.sh can send APNs for the
    # App Store iOS app, so a different upstream will not wake a locked iPhone —
    # but "wrong for iOS" is not "invalid config": an Android-only deployment, or
    # a self-built iOS app carrying its own APNs credentials, legitimately points
    # elsewhere. Refusing those to protect an iOS operator would break topologies
    # this check has no way to distinguish. Flagging costs nothing and is the only
    # honest option, since the failure it warns about is otherwise silent.
    if host and host.lower() not in ("ntfy.sh", "www.ntfy.sh"):
        evidence.append(
            f"NOTE: upstream host is {host!r}, not ntfy.sh — only ntfy.sh can send "
            "APNs for the App Store iOS app, so a LOCKED iPhone will not be woken "
            "by this upstream. Correct for Android or a self-built iOS app; "
            "verify with a locked-phone delivery either way"
        )
    return evidence


# The IF-10 egress relay (SRS-NOTIF-001). Its settings are a CONTRACT with
# crates/atp-adapters/src/notification/smtp.rs, which refuses a relay that
# violates any of them — so a drifted relay is a silently dead alert path.
_EGRESS_SERVICE = "phase1-notification-egress"
_EGRESS_ENTRYPOINT = "docker/notification-egress-entrypoint.sh"
_EGRESS_DOCKERFILE = "docker/notification-egress.Dockerfile"
# The one catalogued secret the relay legitimately holds: the password the
# adapter authenticates with. Every other catalogued secret in its environment
# is an over-exposure.
_EGRESS_PERMITTED_SECRET = "ATP_SMTP_API_KEY"


def _strip_yaml_comments(block: str) -> str:
    """Drop comment text so a rule is not satisfied (or broken) by prose.

    The egress service's own comments say the words ``ports``,
    ``permit_mynetworks`` and ``atp-env`` while EXPLAINING why they are absent.
    A substring check over the raw block reads those as the settings themselves —
    the same trap ``_service_has_vault_mount`` documents for ``/run/atp-secrets``.
    """

    kept = []
    for line in block.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        kept.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(kept)


def _catalogued_secret_names(config: dict) -> list[str]:
    """Every ``secret: true`` key, read from the catalogue rather than restated.

    Enumerating from the source is what makes this rule survive a new secret
    being added: a hardcoded list would silently stop covering it
    (docs/playbooks/adversarial-precheck.md rule 0).
    """

    keys = config.get("configuration", {}).get("keys", [])
    return [key["name"] for key in keys if key.get("secret")]


def assert_notification_egress_relay(config: dict, root: Path = ROOT) -> list[str]:
    """The IF-10 relay must match what ``smtp.rs`` will accept, and hold no more.

    Four rules, each with a fixture proving the check can fail. The first three
    are the constraints the adapter enforces at runtime; the fourth is the
    isolation that keeps a Postfix container from holding the whole vault.
    """

    compose_text = (root / deployment_config(config)["compose_file"]).read_text(encoding="utf-8")
    block = _service_block(compose_text, _EGRESS_SERVICE)
    if block is None:
        fail(f"compose file is missing the IF-10 egress relay service: {_EGRESS_SERVICE}")
    body = _strip_yaml_comments(block)

    entrypoint_path = root / _EGRESS_ENTRYPOINT
    if not entrypoint_path.exists():
        fail(f"egress relay entrypoint is missing: {_EGRESS_ENTRYPOINT}")
    entrypoint = _strip_yaml_comments(entrypoint_path.read_text(encoding="utf-8"))

    # Rule 1 — the EHLO capability list must advertise AUTH (smtp.rs:200).
    if "smtpd_sasl_auth_enable = yes" not in entrypoint:
        fail(
            f"{_EGRESS_ENTRYPOINT} does not set `smtpd_sasl_auth_enable = yes`. "
            "The adapter refuses a relay whose EHLO does not advertise AUTH, "
            "because an open relay lets any container that can route to it forge "
            "operator alerts (smtp.rs:200)."
        )

    # Rule 2 — AUTH must be offered on a PLAINTEXT connection. Postfix's default
    # withholds it until STARTTLS, which the adapter never issues.
    if "smtpd_tls_auth_only = no" not in entrypoint:
        fail(
            f"{_EGRESS_ENTRYPOINT} does not set `smtpd_tls_auth_only = no`. Postfix "
            "then withholds AUTH from the capability list until STARTTLS, which the "
            "adapter never issues — so every alert fails Unconfigured while the "
            "relay configuration looks correct."
        )

    # Rule 3 — no permit_mynetworks on the submission path.
    restriction_lines = [
        line
        for line in entrypoint.splitlines()
        if "smtpd_relay_restrictions" in line or "smtpd_recipient_restrictions" in line
    ]
    if len(restriction_lines) < 2:
        fail(
            f"{_EGRESS_ENTRYPOINT} must set BOTH smtpd_relay_restrictions and "
            "smtpd_recipient_restrictions; found: " + "; ".join(restriction_lines)
        )
    for line in restriction_lines:
        if "permit_mynetworks" in line:
            fail(
                f"{_EGRESS_ENTRYPOINT} permits mynetworks on the submission path "
                f"({line.strip()}). Requiring AUTH is the point: with this, any peer "
                "that can route to the relay can forge an operator alert without the "
                "credential."
            )
        if "permit_sasl_authenticated" not in line:
            fail(
                f"{_EGRESS_ENTRYPOINT} restriction does not require SASL "
                f"authentication: {line.strip()}"
            )

    # Rule 4 — isolation. The relay is not an ATP process: it publishes no host
    # port, merges no ATP environment, mounts no vault, and holds exactly one
    # catalogued secret.
    if re.search(r"^\s*ports:", body, re.MULTILINE):
        fail(
            f"{_EGRESS_SERVICE} publishes a host port. The submission endpoint must "
            "be reachable only from peers on the compose network; a host-published "
            "port is still an RFC 1918 address, so EgressEndpoint's private-address "
            "rule does not constrain who reaches it."
        )
    if "*atp-env" in body:
        fail(
            f"{_EGRESS_SERVICE} merges *atp-env. That hands a third-party MTA every "
            "catalogued secret — the IB account, the data-vendor keys, the ntfy topic "
            "and token — when it needs exactly one."
        )
    # `body`, not `block`: this service's own comment says the words
    # `*atp-volumes` and `/run/atp-secrets` while explaining why it uses
    # NEITHER, and the raw block reads those as the settings themselves.
    if _service_has_vault_mount(body):
        fail(
            f"{_EGRESS_SERVICE} mounts the SRS-SEC-001 credential vault. It cannot "
            "read the vault format, and mounting it exposes every sealed secret to a "
            "container that needs one of them."
        )
    leaked = [
        name
        for name in _catalogued_secret_names(config)
        if name != _EGRESS_PERMITTED_SECRET
        and re.search(rf"^\s*{re.escape(name)}:", body, re.MULTILINE)
    ]
    if leaked:
        fail(
            f"{_EGRESS_SERVICE} receives catalogued secrets it does not need: "
            + ", ".join(sorted(leaked))
        )

    # Rule 5 — the provider password is a secret and must not live in the
    # environment where `docker inspect` and every child process can read it.
    if "ATP_EGRESS_PROVIDER_PASSWORD_FILE" not in body:
        fail(
            f"{_EGRESS_SERVICE} does not point at a provider-password FILE. An "
            "environment variable is readable by `docker inspect` and inherited by "
            "every child process, while every other notification credential is "
            "catalogued and sealed."
        )
    if "/run/atp-secrets" in body:
        fail(
            f"{_EGRESS_SERVICE} mounts the shared secrets directory. It needs one "
            "file; mounting the directory hands a Postfix container the sealed "
            "vault and its key as well."
        )
    # Match the REFUSAL ITSELF, not the two tokens somewhere in the file.
    # `is_production_env` is also the name of a helper used by the placeholder
    # rule, so a presence check passed happily while the guard was disabled —
    # the "checklist-shaped guard" weakness in
    # docs/playbooks/adversarial-precheck.md rule 0.
    guards_env_form = any(
        "is_production_env" in line and "PROVIDER_PASSWORD_SOURCE" in line
        for line in entrypoint.splitlines()
    )
    if not guards_env_form:
        fail(
            f"{_EGRESS_ENTRYPOINT} no longer refuses an environment-sourced provider "
            "password in staging/production. The env form is a development "
            "convenience only."
        )

    dockerfile = (root / _EGRESS_DOCKERFILE).read_text(encoding="utf-8")
    if not re.search(r"^FROM\s+\S+@sha256:[0-9a-f]{64}", dockerfile, re.MULTILINE):
        fail(
            f"{_EGRESS_DOCKERFILE} does not pin its base image by digest. This "
            "container sits on the alert path with `restart: unless-stopped`, so a "
            "floating tag lets a later pull swap the base underneath it."
        )

    return [
        f"{_EGRESS_SERVICE} advertises AUTH on the plaintext hop "
        "(smtpd_sasl_auth_enable=yes, smtpd_tls_auth_only=no) as smtp.rs requires",
        f"{_EGRESS_SERVICE} requires SASL on both restriction lists and never permits mynetworks",
        f"{_EGRESS_SERVICE} publishes no host port, merges no ATP environment, "
        f"mounts no vault, and holds only {_EGRESS_PERMITTED_SECRET}",
        f"{_EGRESS_SERVICE} takes its provider password from a single-file "
        "read-only projection, and the entrypoint refuses the environment form "
        "in staging/production",
        f"{_EGRESS_DOCKERFILE} pins its base image by digest",
    ]


def assert_deployment_static(config: dict, root: Path = ROOT) -> list[str]:
    evidence: list[str] = [
        "SRS-ARCH-004 Phase 1 deployment evidence:",
    ]
    evidence.extend(assert_compose_services(config, root))
    evidence.extend(assert_compose_env_and_volumes(config, root))
    evidence.extend(assert_dockerfiles_present(config, root))
    evidence.extend(assert_env_example(config, root))
    evidence.extend(assert_deployment_doc(config, root))
    evidence.extend(assert_credential_vault_wiring(config, root))
    evidence.extend(assert_ntfy_upstream_well_formed(config, root))
    evidence.extend(assert_notification_egress_relay(config, root))
    return evidence


# Each entry rewrites ONE line into a violation of exactly one rule. Every one
# of these produces a relay that starts cleanly, so none is caught by anything
# but the check itself (docs/playbooks/security-boundaries.md rule 22).
_EGRESS_ENTRYPOINT_FIXTURES = {
    "egress-no-auth": (
        'postconf -e "smtpd_sasl_auth_enable = yes"',
        'postconf -e "smtpd_sasl_auth_enable = no"',
    ),
    "egress-tls-auth-only": (
        'postconf -e "smtpd_tls_auth_only = no"',
        'postconf -e "smtpd_tls_auth_only = yes"',
    ),
    "egress-plaintext-provider-password": (
        'if is_production_env && [ "$PROVIDER_PASSWORD_SOURCE" !=',
        'if false && [ "$PROVIDER_PASSWORD_SOURCE" !=',
    ),
    "egress-permit-mynetworks": (
        'postconf -e "smtpd_relay_restrictions = permit_sasl_authenticated, reject"',
        'postconf -e "smtpd_relay_restrictions = permit_mynetworks, permit_sasl_authenticated, reject"',
    ),
}
_EGRESS_COMPOSE_FIXTURES = {
    "egress-published-port": (
        '  phase1-notification-egress:\n    profiles: ["phase1"]\n',
        '  phase1-notification-egress:\n    profiles: ["phase1"]\n'
        '    ports:\n      - "127.0.0.1:1025:1025"\n',
    ),
    "egress-atp-env": (
        "    environment:\n      ATP_ENV: ${ATP_ENV:-development}\n"
        "      ATP_SMTP_API_KEY: ${ATP_SMTP_API_KEY:-placeholder-set-in-environment}\n",
        "    environment: *atp-env\n",
    ),
}


def make_fixture_root(fixture: str) -> tempfile.TemporaryDirectory[str]:
    temp_dir = tempfile.TemporaryDirectory()
    temp_root = Path(temp_dir.name)
    (temp_root / "architecture").mkdir()
    (temp_root / "docker").mkdir()
    (temp_root / "docs").mkdir()

    shutil.copy2(CONFIG_PATH, temp_root / "architecture" / "runtime_services.json")
    shutil.copy2(ROOT / "docker-compose.yml", temp_root / "docker-compose.yml")
    shutil.copy2(ROOT / ".env.example", temp_root / ".env.example")
    shutil.copy2(ROOT / "docs" / "DEPLOYMENT.md", temp_root / "docs" / "DEPLOYMENT.md")
    # DERIVED from the manifest, not restated. The hardcoded tuple this replaces
    # would have silently stopped copying a newly registered Dockerfile, and the
    # fixture run would then fail for the wrong reason — a check reporting on its
    # own blind spot (docs/playbooks/test-integrity.md rule 8).
    config = json.loads((ROOT / "architecture" / "runtime_services.json").read_text("utf-8"))
    for dockerfile in config["deployment"]["required_dockerfiles"]:
        shutil.copy2(ROOT / dockerfile, temp_root / dockerfile)
    shutil.copy2(ROOT / _EGRESS_ENTRYPOINT, temp_root / _EGRESS_ENTRYPOINT)

    compose_path = temp_root / "docker-compose.yml"
    if fixture == "missing-jupyter":
        text = compose_path.read_text(encoding="utf-8")
        text = re.sub(
            r"\n  phase1-jupyter:\n(?:    [^\n]*\n|    \n|      [^\n]*\n)+",
            "\n",
            text,
        )
        compose_path.write_text(text, encoding="utf-8")
    elif fixture == "missing-ssd":
        text = compose_path.read_text(encoding="utf-8")
        text = text.replace("ATP_SSD_DATA_DIR", "ATP_REMOVED_FOR_FIXTURE")
        compose_path.write_text(text, encoding="utf-8")
    elif fixture == "missing-portability-doc":
        (temp_root / "docs" / "DEPLOYMENT.md").write_text(
            "# Deployment\n\nTBD.\n", encoding="utf-8"
        )
    elif fixture in _EGRESS_ENTRYPOINT_FIXTURES:
        # One bypass class each, all four expressible in a relay that starts
        # cleanly and passes every scripted-relay test.
        entrypoint_path = temp_root / _EGRESS_ENTRYPOINT
        text = entrypoint_path.read_text(encoding="utf-8")
        old_line, new_line = _EGRESS_ENTRYPOINT_FIXTURES[fixture]
        if old_line not in text:
            temp_dir.cleanup()
            raise ValueError(
                f"fixture {fixture!r} cannot find its anchor {old_line!r} in "
                f"{_EGRESS_ENTRYPOINT} — the fixture is inert, which reads exactly "
                "like the check passing"
            )
        entrypoint_path.write_text(text.replace(old_line, new_line), encoding="utf-8")
    elif fixture in _EGRESS_COMPOSE_FIXTURES:
        compose = temp_root / "docker-compose.yml"
        text = compose.read_text(encoding="utf-8")
        old_line, new_line = _EGRESS_COMPOSE_FIXTURES[fixture]
        if old_line not in text:
            temp_dir.cleanup()
            raise ValueError(
                f"fixture {fixture!r} cannot find its anchor {old_line!r} in "
                "docker-compose.yml — the fixture is inert"
            )
        compose.write_text(text.replace(old_line, new_line, 1), encoding="utf-8")
    else:
        temp_dir.cleanup()
        raise ValueError(f"unknown fixture: {fixture}")

    return temp_dir


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        choices=[
            "missing-jupyter",
            "missing-ssd",
            "missing-portability-doc",
            *_EGRESS_ENTRYPOINT_FIXTURES,
            *_EGRESS_COMPOSE_FIXTURES,
        ],
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
        evidence = assert_deployment_static(config, root)
    except DeploymentCheckError as error:
        print(f"SRS-ARCH-004 FAIL: {error}", file=sys.stderr)
        return 1
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    print("SRS-ARCH-004 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
