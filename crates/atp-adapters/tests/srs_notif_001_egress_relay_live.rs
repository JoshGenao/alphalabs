//! L4 boundary (live) — SRS-NOTIF-001 IF-10 against a REAL
//! `phase1-notification-egress` relay.
//!
//! WHY THIS EXISTS SEPARATELY FROM `srs_notif_001_transports.rs`. That file
//! drives the adapter against a SCRIPTED relay: a test double written to match
//! what `smtp.rs` expects. It cannot tell us whether *Postfix* behaves that way,
//! and the gap between "the adapter is correct" and "the adapter and the relay
//! agree" is exactly where a serialized feature's first real run fails (see
//! docs/playbooks/scope-and-serialization.md rule 7).
//!
//! The single defect this catches, which nothing else can: Postfix's default
//! `smtpd_tls_auth_only = yes` withholds AUTH from the EHLO capability list
//! until STARTTLS. The adapter never issues STARTTLS, so it would hit
//! `smtp.rs:200` and refuse to submit — with every setting in the relay's
//! configuration looking correct, and every scripted test still green.
//!
//! `#[ignore]` because it needs a relay listening. It is NOT gated on
//! `ATP_RUN_INTEGRATION`: an unset environment must FAIL here rather than skip,
//! because a silent skip on the alert path reads exactly like a pass.
//!
//! Run it:
//! ```text
//! docker build -f docker/notification-egress.Dockerfile -t atp-notification-egress:dev .
//! # The provider password is a FILE, never an env var. The entrypoint refuses
//! # to start if ATP_EGRESS_PROVIDER_PASSWORD is set, and refuses to start if
//! # the file is missing or empty — so the older `-e ...PASSWORD=` recipe that
//! # stood here could not have worked.
//! install -m 600 /dev/null /tmp/atp-egress-probe-password
//! printf '%s' '<provider key>' > /tmp/atp-egress-probe-password
//! docker run -d --name atp-egress-probe -p 127.0.0.1:21025:1025 \
//!     -v /tmp/atp-egress-probe-password:/run/egress-secrets/provider-password:ro \
//!     -e ATP_SMTP_SENDER=... -e ATP_SMTP_RELAY_USER=... -e ATP_SMTP_API_KEY=... \
//!     -e ATP_EGRESS_PROVIDER_HOST=... -e ATP_EGRESS_PROVIDER_USER=... \
//!     atp-notification-egress:dev
//! ATP_EGRESS_LIVE_HOST=127.0.0.1 ATP_EGRESS_LIVE_PORT=21025 \
//! ATP_EGRESS_LIVE_USER=... ATP_EGRESS_LIVE_KEY=... \
//! ATP_EGRESS_LIVE_SENDER=... ATP_EGRESS_LIVE_RECIPIENT=... \
//!     cargo test -p atp-adapters --test srs_notif_001_egress_relay_live -- --ignored --nocapture
//! ```

use std::time::Duration;

use atp_adapters::notification::{SmtpEmailChannel, SmtpRelayConfig};
use atp_notification::{
    ChannelError, NotificationChannel, NotificationChannelClient, NotificationMessage,
};

/// Read a required variable, failing loudly rather than skipping.
fn required(name: &str) -> String {
    match std::env::var(name) {
        Ok(value) if !value.is_empty() => value,
        _ => panic!(
            "{name} is unset. This test drives a REAL relay and has no meaningful \
             default; an absent endpoint is a configuration error, not a pass."
        ),
    }
}

struct LiveRelay {
    host: String,
    port: u16,
    user: String,
    key: String,
    sender: String,
    recipient: String,
}

impl LiveRelay {
    fn from_env() -> Self {
        let port = required("ATP_EGRESS_LIVE_PORT");
        Self {
            host: required("ATP_EGRESS_LIVE_HOST"),
            port: port
                .parse()
                .unwrap_or_else(|_| panic!("ATP_EGRESS_LIVE_PORT is not a port: {port:?}")),
            user: required("ATP_EGRESS_LIVE_USER"),
            key: required("ATP_EGRESS_LIVE_KEY"),
            sender: required("ATP_EGRESS_LIVE_SENDER"),
            recipient: required("ATP_EGRESS_LIVE_RECIPIENT"),
        }
    }

    fn channel(&self, key: &str) -> SmtpEmailChannel {
        SmtpEmailChannel::new(
            SmtpRelayConfig::new(
                &self.host,
                self.port,
                &self.sender,
                &self.recipient,
                &self.user,
                key,
            )
            .expect("live relay config is structurally valid"),
        )
    }
}

fn alert() -> NotificationMessage {
    NotificationMessage::new(
        "CRITICAL: IB connectivity lost",
        "SRS-NOTIF-001 live egress-relay check. IB Gateway unreachable.",
    )
}

/// The whole point: the REAL relay advertises AUTH on a plaintext connection and
/// accepts the adapter's full conversation.
#[test]
#[ignore = "needs a running phase1-notification-egress; see the module docs"]
fn the_real_relay_advertises_auth_and_accepts_the_adapters_submission() {
    let relay = LiveRelay::from_env();
    let channel = relay.channel(&relay.key);

    assert_eq!(channel.channel(), NotificationChannel::Email);

    match channel.send(&alert(), Duration::from_secs(30)) {
        Ok(receipt) => {
            println!("relay accepted the submission: {}", receipt.reference());
            assert!(
                !receipt.reference().is_empty(),
                "an accepted submission must carry the relay's own reply as its \
                 durable reference — an empty one stores no audit trail"
            );
            assert!(
                !receipt.reference().contains(&relay.key),
                "the relay echoed the credential into the reply that becomes the \
                 STORED receipt — redaction failed on the happy path"
            );
        }
        Err(ChannelError::Unconfigured { detail })
            if detail.contains("does not advertise AUTH") =>
        {
            panic!(
                "the relay does not advertise AUTH on a plaintext connection. This is \
                 the `smtpd_tls_auth_only` trap: Postfix withholds AUTH until STARTTLS \
                 by default, and the adapter never issues it. detail: {detail}"
            );
        }
        Err(other) => panic!("live submission failed: {other:?}"),
    }
}

/// AUTH must be ENFORCED, not merely advertised. A relay that announces AUTH and
/// then accepts anyone is the open relay this whole design exists to refuse.
#[test]
#[ignore = "needs a running phase1-notification-egress; see the module docs"]
fn the_real_relay_refuses_a_wrong_credential() {
    let relay = LiveRelay::from_env();
    let channel = relay.channel("definitely-not-the-configured-key");

    match channel.send(&alert(), Duration::from_secs(30)) {
        Err(ChannelError::Rejected { detail, .. }) => {
            println!("relay rejected the bad credential as expected: {detail}");
            assert!(
                !detail.contains("definitely-not-the-configured-key"),
                "the rejection detail carries the credential we just sent — it is \
                 PERSISTED, so this would write recoverable secret material to disk"
            );
        }
        Ok(_) => panic!(
            "the relay ACCEPTED a wrong credential. It is an open relay: any container \
             that can route to it can forge an operator alert."
        ),
        Err(other) => panic!("expected Rejected for a bad credential, got {other:?}"),
    }
}
