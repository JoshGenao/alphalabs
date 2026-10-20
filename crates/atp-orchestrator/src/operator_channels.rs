//! The operator notification transports, built from the environment, shared by every operator
//! alert binary (SRS-NOTIF-001's outage alert, SRS-DATA-013's quarantine alert).
//!
//! One copy on purpose: the placeholder-credential refusal is a safety check, and two binaries
//! each carrying their own copy would let one drift from the other.

use atp_adapters::notification::{PushChannel, PushConfig, SmtpEmailChannel, SmtpRelayConfig};
use atp_notification::{ChannelError, SharedChannelClient};

/// The literal the catalogue ships as every secret's default, and which
/// `.env.example` tells the operator to LEAVE IN PLACE when the real values are
/// sealed in the SRS-SEC-001 vault.
pub const PLACEHOLDER_SECRET: &str = "placeholder-set-in-environment";

/// Deployment modes where a placeholder credential is an error, matching
/// `atp_config`'s `PRODUCTION_ENVS`.
pub const PRODUCTION_ENVS: [&str; 2] = ["staging", "production"];

/// Refuse to build transports from placeholder credentials in staging/production.
///
/// This binary reads the process environment directly, and it CANNOT overlay the
/// vault: the vault is `atp_config.vault`, a Python component, and this is the
/// Rust composition root. That is fine as a division of labour — but it leaves a
/// gap the documented deployment flow walks straight into. `.env.example` tells
/// the operator to seal ATP_PUSH_TOPIC / ATP_PUSH_TOKEN / ATP_SMTP_API_KEY in the
/// vault and LEAVE THE PLACEHOLDERS in `.env`. Without this check, running the
/// operator alert in that (correct) configuration would build transports from the
/// literal string `placeholder-set-in-environment` and try to publish with it.
///
/// So the binary enforces the half of the readiness contract it can: the same
/// placeholder rejection `atp_config` applies, at the same severity, in the same
/// environments. It names the offending keys and never prints their values.
pub fn reject_placeholder_secrets(read: &impl Fn(&str) -> Option<String>) -> Result<(), String> {
    let env = read("ATP_ENV").unwrap_or_default();
    if !PRODUCTION_ENVS.contains(&env.trim()) {
        return Ok(());
    }
    let offenders: Vec<&str> = ["ATP_SMTP_API_KEY", "ATP_PUSH_TOPIC", "ATP_PUSH_TOKEN"]
        .into_iter()
        .filter(|key| read(key).as_deref() == Some(PLACEHOLDER_SECRET))
        .collect();
    if offenders.is_empty() {
        return Ok(());
    }
    Err(format!(
        "ATP_ENV={env} but {} still hold the catalogue placeholder. The real values \
         are sealed in the SRS-SEC-001 vault, which this Rust binary cannot open — \
         export them into the environment for this run (see docs/DEPLOYMENT.md). \
         Refusing to publish an operator alert with placeholder credentials.",
        offenders.join(", ")
    ))
}

pub fn build_channels() -> Result<Vec<SharedChannelClient>, ChannelError> {
    let read = |key: &str| std::env::var(key).ok();
    let email = SmtpEmailChannel::new(SmtpRelayConfig::from_env(read)?);
    let push = PushChannel::new(PushConfig::from_env(read)?);
    Ok(vec![
        std::sync::Arc::new(email) as SharedChannelClient,
        std::sync::Arc::new(push) as SharedChannelClient,
    ])
}
