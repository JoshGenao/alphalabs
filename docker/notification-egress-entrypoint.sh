#!/bin/sh
# phase1-notification-egress startup: render Postfix from environment, then run
# it in the foreground. SRS-NOTIF-001 (IF-10), SRS-SEC-001 (NFR-S4).
#
# Nothing here is baked into an image layer — every credential arrives as
# environment at `docker run` time and is written to a file inside the running
# container only. `set -u` is deliberate: an unset variable is a configuration
# bug, and this container is on the alert path.
#
# NO CREDENTIAL IS EVER ECHOED. The values are passed to `saslpasswd2` on stdin
# and written to sasl_passwd with a redirect, never interpolated into a command
# line (which `ps` would expose to every process in the container) and never
# printed. Same rule the transports follow — see the `command_redacted` note in
# progress.d/session-SRS-NOTIF-001.md for why the weaker version was deleted.
set -eu

log() { echo "phase1-notification-egress: $*"; }
die() { echo "phase1-notification-egress: FATAL: $*" >&2; exit 1; }

PLACEHOLDER="placeholder-set-in-environment"
ATP_ENV="${ATP_ENV:-development}"

# The inbound submission port. Must match the adapter's ATP_SMTP_RELAY_PORT
# (crates/atp-adapters/src/notification/smtp.rs DEFAULT_RELAY_PORT = 1025).
RELAY_PORT="${ATP_SMTP_RELAY_PORT:-1025}"

# The SASL identity the ADAPTER authenticates as. smtp.rs falls back to
# ATP_SMTP_SENDER when ATP_SMTP_RELAY_USER is unset, so mirror that exactly —
# a relay that disagrees with the adapter about the username authenticates
# nobody, and the failure surfaces as a bare 535 with no hint why.
RELAY_USER="${ATP_SMTP_RELAY_USER:-${ATP_SMTP_SENDER:-}}"
RELAY_PASSWORD="${ATP_SMTP_API_KEY:-}"

# The real provider this relay forwards to over TLS (Brevo, Gmail, ...).
PROVIDER_HOST="${ATP_EGRESS_PROVIDER_HOST:-}"
PROVIDER_PORT="${ATP_EGRESS_PROVIDER_PORT:-587}"
PROVIDER_USER="${ATP_EGRESS_PROVIDER_USER:-}"
PROVIDER_PASSWORD="${ATP_EGRESS_PROVIDER_PASSWORD:-}"

# --- configuration gate ------------------------------------------------------
#
# Missing and placeholder are DIFFERENT states and neither is "empty" (CLAUDE.md
# rule 3). In staging/production both are fatal; in development a placeholder is
# a warning so a dev bring-up still starts, matching the precedent set for the
# other notification credentials (commit da73caf).
is_production_env() {
    [ "$ATP_ENV" = "staging" ] || [ "$ATP_ENV" = "production" ]
}

require() {
    # require <human name> <value>
    name="$1"
    value="$2"
    if [ -z "$value" ]; then
        die "$name is not set. This relay is on the SYS-46 alert path and will not
    start half-configured: an accepted message that can never be delivered is
    indistinguishable to the adapter from a delivered one."
    fi
    if [ "$value" = "$PLACEHOLDER" ]; then
        if is_production_env; then
            die "$name is still the catalogue placeholder ($PLACEHOLDER) and ATP_ENV
    is '$ATP_ENV'. Refusing to start."
        fi
        log "WARNING: $name is still the catalogue placeholder. ATP_ENV is
    '$ATP_ENV', so this is allowed — but no alert can actually be delivered."
    fi
}

require "ATP_SMTP_RELAY_USER (or ATP_SMTP_SENDER)" "$RELAY_USER"
require "ATP_SMTP_API_KEY" "$RELAY_PASSWORD"
require "ATP_EGRESS_PROVIDER_HOST" "$PROVIDER_HOST"
require "ATP_EGRESS_PROVIDER_USER" "$PROVIDER_USER"
require "ATP_EGRESS_PROVIDER_PASSWORD" "$PROVIDER_PASSWORD"

case "$RELAY_PORT" in
    ''|*[!0-9]*) die "ATP_SMTP_RELAY_PORT is not numeric: '$RELAY_PORT'" ;;
esac
case "$PROVIDER_PORT" in
    ''|*[!0-9]*) die "ATP_EGRESS_PROVIDER_PORT is not numeric: '$PROVIDER_PORT'" ;;
esac

MYHOSTNAME="phase1-notification-egress"

# --- inbound SASL (Cyrus sasldb) ---------------------------------------------
#
# The realm is pinned to $MYHOSTNAME rather than left to Cyrus's default,
# because the default is the container's FQDN — which changes with the compose
# project name and would silently break authentication that worked yesterday.
mkdir -p /etc/postfix/sasl
cat > /etc/postfix/sasl/smtpd.conf <<EOF
pwcheck_method: auxprop
auxprop_plugin: sasldb
mech_list: PLAIN LOGIN
EOF

rm -f /etc/sasldb2
printf '%s' "$RELAY_PASSWORD" | saslpasswd2 -p -c -u "$MYHOSTNAME" "$RELAY_USER"
chown postfix:postfix /etc/sasldb2
chmod 600 /etc/sasldb2

# --- outbound provider credential --------------------------------------------
umask 077
printf '[%s]:%s %s:%s\n' \
    "$PROVIDER_HOST" "$PROVIDER_PORT" "$PROVIDER_USER" "$PROVIDER_PASSWORD" \
    > /etc/postfix/sasl_passwd
postmap /etc/postfix/sasl_passwd
chmod 600 /etc/postfix/sasl_passwd /etc/postfix/sasl_passwd.db
umask 022

# --- main.cf ------------------------------------------------------------------
postconf -e "myhostname = $MYHOSTNAME"
postconf -e "mydestination ="
postconf -e "inet_interfaces = all"
postconf -e "inet_protocols = ipv4"
postconf -e "maillog_file = /dev/stdout"

# Constraint 1: the EHLO capability list must advertise AUTH (smtp.rs:200).
postconf -e "smtpd_sasl_auth_enable = yes"
postconf -e "smtpd_sasl_type = cyrus"
postconf -e "smtpd_sasl_path = smtpd"
postconf -e "smtpd_sasl_local_domain = $MYHOSTNAME"

# Constraint 2: AUTH PLAIN, offered on a PLAINTEXT connection.
#
# `smtpd_tls_auth_only = no` IS LOAD-BEARING AND EASY TO MISREAD AS A WEAKENING.
# The adapter never issues STARTTLS, so with the default `yes` Postfix withholds
# AUTH from the capability list until TLS is up; the adapter then sees no AUTH,
# hits smtp.rs:200 and refuses to submit — every setting looking correct while
# no alert can ever be sent. The cleartext hop is acceptable ONLY because
# EgressEndpoint has already proven it terminates on loopback / RFC 1918, and
# the TLS that matters (to the provider) is on the OUTBOUND leg below.
postconf -e "smtpd_tls_auth_only = no"
postconf -e "smtpd_sasl_security_options = noanonymous"

# Constraint 3: NO `permit_mynetworks` on the submission path. Requiring AUTH is
# the entire point — an open relay lets any container that can route here forge
# an operator alert. `mynetworks` is narrowed too, so that a future edit which
# re-adds permit_mynetworks does less damage than it otherwise would.
postconf -e "mynetworks = 127.0.0.0/8"
postconf -e "smtpd_relay_restrictions = permit_sasl_authenticated, reject"
postconf -e "smtpd_recipient_restrictions = permit_sasl_authenticated, reject"

# The adapter greets as `EHLO atp-notification` — a valid but non-FQDN name.
# Postfix accepts it by default; pinned explicitly so a future hardening pass
# that adds reject_non_fqdn_helo_hostname has to notice this line first.
postconf -e "smtpd_helo_restrictions ="

# Outbound: authenticated TLS to the real provider. This is the leg the whole
# container exists for.
postconf -e "relayhost = [$PROVIDER_HOST]:$PROVIDER_PORT"
postconf -e "smtp_sasl_auth_enable = yes"
postconf -e "smtp_sasl_password_maps = hash:/etc/postfix/sasl_passwd"
postconf -e "smtp_sasl_security_options = noanonymous"
postconf -e "smtp_tls_security_level = encrypt"
postconf -e "smtp_tls_CAfile = /etc/ssl/certs/ca-certificates.crt"

# --- master.cf ----------------------------------------------------------------
#
# Listen ONLY on the submission port, and only unchrooted (the SASL database
# lives outside /var/spool/postfix). Removing the default port-25 listener keeps
# the reachable surface to exactly the one the adapter uses.
# CHROOT OFF FOR EVERY DAEMON. Debian ships master.cf with the chroot column
# set to `y`, which breaks this container in two ways that both look like
# something else:
#   * the smtp client cannot read /etc/resolv.conf, so the provider hostname
#     fails to resolve and every message sits deferred with
#     `Name service error for name=... type=A: Host not found, try again` —
#     which reads as a DNS or network problem, not a Postfix setting;
#   * it cannot read /etc/ssl/certs/ca-certificates.crt either, so
#     `smtp_tls_security_level = encrypt` would fail to verify the provider.
# Both were caught by the first real run against a real relay; neither is
# visible to a scripted-relay test. The chroot buys little here regardless —
# the container is the isolation boundary.
postconf -F '*/*/chroot = n'

postconf -M -X "smtp/inet" 2>/dev/null || true
postconf -M -X "${RELAY_PORT}/inet" 2>/dev/null || true
postconf -M -e "${RELAY_PORT}/inet=${RELAY_PORT} inet n - n - - smtpd"

log "relay ready on :${RELAY_PORT}, forwarding to [${PROVIDER_HOST}]:${PROVIDER_PORT}"
log "inbound SASL user '${RELAY_USER}' in realm '${MYHOSTNAME}' (AUTH PLAIN over the private hop)"

exec postfix start-fg
