# phase1-notification-egress — the IF-10 email egress relay (SRS-NOTIF-001).
#
# WHY THIS CONTAINER EXISTS. The Rust workspace carries ZERO external crates
# (SRS-ARCH-001 / C-12), so a std-only adapter has no TLS implementation. Rather
# than break that invariant tree-wide for one channel, the TLS boundary was made
# a DEPLOYMENT component: `SmtpEmailChannel` speaks PLAINTEXT SMTP to this relay
# over a private hop, and this relay owns the authenticated TLS session to the
# real provider.
#
# THE ADAPTER REFUSES A RELAY THAT DOES NOT MATCH IT. These are contract, not
# preference — each is enforced in crates/atp-adapters/src/notification/smtp.rs:
#
#   1. The EHLO capability list MUST advertise AUTH (smtp.rs:200). Otherwise the
#      send fails Unconfigured: an open relay would let any container that can
#      route to it forge operator alerts. This is the constraint most likely to
#      be missed, because a relay works fine for `swaks` without it.
#   2. AUTH PLAIN is the only mechanism implemented (smtp.rs:222), and it is
#      offered on a PLAINTEXT connection — the adapter never issues STARTTLS.
#      That is why the entrypoint sets `smtpd_tls_auth_only = no`; Postfix
#      otherwise withholds AUTH from the capability list until TLS is up, which
#      trips constraint 1 while every setting still looks correct.
#   3. The relay must resolve to loopback / RFC 1918. `EgressEndpoint`
#      re-resolves and re-validates on EVERY connect, so a compose service name
#      is fine (it resolves to the bridge subnet) but a public address is not.
#
# Postfix is configured as a RELAY-ONLY SATELLITE: it accepts nothing without
# SASL authentication, delivers nothing locally, and forwards everything to the
# provider over TLS. See docker/notification-egress-entrypoint.sh for the
# rendered configuration and docs/DEPLOYMENT.md for the runbook.

# PINNED BY DIGEST, not by a floating tag. This container sits on the alert
# path; with `restart: unless-stopped` a later `docker compose pull` could
# otherwise swap the base underneath it. Same reasoning as phase1-ntfy's
# version pin in docker-compose.yml.
FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171

# Debian's postfix package asks debconf questions on install. Preseed them:
# "No configuration" leaves main.cf to the entrypoint, which renders the whole
# file from environment at start so no credential is ever baked into a layer.
RUN echo "postfix postfix/main_mailer_type select No configuration" \
        | debconf-set-selections \
    && DEBIAN_FRONTEND=noninteractive apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        postfix \
        libsasl2-modules \
        libsasl2-modules-db \
        sasl2-bin \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The "No configuration" preseed above deliberately leaves /etc/postfix/main.cf
# absent, and EVERY postfix tool — postconf, postmap — dies on that with
# `fatal: open /etc/postfix/main.cf: No such file or directory`. Seed it from
# Debian's own template so the entrypoint has a base to overwrite. Build time,
# not run time: the template holds no credential.
RUN cp /usr/share/postfix/main.cf.debian /etc/postfix/main.cf

COPY docker/notification-egress-entrypoint.sh /usr/local/bin/notification-egress-entrypoint.sh
RUN chmod +x /usr/local/bin/notification-egress-entrypoint.sh

# The submission port the adapter connects to (smtp.rs DEFAULT_RELAY_PORT).
# Documentation only — compose publishes NOTHING to the host for this service.
EXPOSE 1025

ENTRYPOINT ["/usr/local/bin/notification-egress-entrypoint.sh"]
