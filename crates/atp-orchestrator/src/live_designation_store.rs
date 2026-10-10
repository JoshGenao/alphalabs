//! The durable single-live designation snapshot (SRS-EXE-001 / SRS-RESV-005; SyRS
//! SYS-2a).
//!
//! ONE file names the strategy that may route to IB, and every process that reads or
//! moves the live slot goes through this module: the Hot-Swap promote CLI
//! (`resv005_hot_swap_promote_cli`), the operator designation CLI
//! (`exe001_live_designation_cli`), and the live execution host
//! (`live_execution_host`), which re-reads it on every order. Two private copies of
//! this format could drift, and a host that disagreed with the Hot-Swap about who is
//! live would route a demoted strategy's orders to IB.
//!
//! Every reader and writer serializes on
//! [`trigger_config_store::ExclusiveGuard`](crate::trigger_config_store::ExclusiveGuard)
//! over the snapshot path, held for the whole read -> decide -> write (or read ->
//! route) sequence. That is the same guard `cmd_swap` already took.
//!
//! The functions below moved here from `resv005_hot_swap_promote_cli.rs`. The one
//! change is that [`load_designation`] now bounds its read, because the host calls
//! it on every order.

use atp_execution::designation::{LiveDesignation, LiveDesignationConfirmation};
use atp_types::StrategyId;
use std::fs;
use std::io::{Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

/// Schema version of the durable live-designation snapshot, embedded in the magic
/// line so an old reader hits a clean version gate rather than "corrupt".
pub const DESIGNATION_STATE_SCHEMA_VERSION: u64 = 1;

/// Magic header compared for exact equality on load; a foreign or truncated file
/// refuses the whole read rather than reading as "nothing is designated" — that
/// silent empty would let a promotion run over a live strategy.
pub const STATE_MAGIC: &str = "RESV005-LIVE-DESIGNATION-STATE v1";

const _: () = {
    assert!(DESIGNATION_STATE_SCHEMA_VERSION == 1);
    assert!(matches!(STATE_MAGIC.as_bytes().last(), Some(b'1')));
};

/// The largest snapshot [`load_designation`] reads. The magic line plus one
/// `designated\t<id>` line is well under 200 bytes.
pub const MAX_SNAPSHOT_BYTES: u64 = 4096;

static SCRATCH_SEQ: AtomicU64 = AtomicU64::new(0);

/// Read the durable designation.
///
/// Three states, kept apart: **no file** = nothing designated (a first run);
/// **a valid snapshot** = whatever it names; **a foreign, truncated, or malformed
/// file** = an ERROR. Collapsing the third into the first is exactly the failure
/// this gate exists to prevent — it would let a promotion proceed as though no
/// strategy were live.
///
/// The read is bounded by [`MAX_SNAPSHOT_BYTES`]: the live execution host calls this
/// on every order, and a valid snapshot is at most two short lines.
pub fn load_designation(path: &Path) -> Result<LiveDesignation, String> {
    let file = match fs::File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(LiveDesignation::new())
        }
        Err(error) => {
            return Err(format!(
                "cannot read state file {}: {error}",
                path.display()
            ))
        }
    };
    let mut content = String::new();
    file.take(MAX_SNAPSHOT_BYTES + 1)
        .read_to_string(&mut content)
        .map_err(|error| format!("cannot read state file {}: {error}", path.display()))?;
    if content.len() as u64 > MAX_SNAPSHOT_BYTES {
        return Err(format!(
            "state file {} is larger than {MAX_SNAPSHOT_BYTES} bytes; a designation snapshot \
             is two short lines, so this is not one",
            path.display()
        ));
    }
    let mut lines = content.lines();
    match lines.next() {
        Some(line) if line == STATE_MAGIC => {}
        _ => {
            return Err(format!(
                "state file {} is not a {STATE_MAGIC} snapshot (refusing a foreign or \
                 truncated file rather than reading it as 'nothing is live')",
                path.display()
            ))
        }
    }
    let mut designation = LiveDesignation::new();
    let mut seen = false;
    for (index, line) in lines.enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let Some(id) = line.strip_prefix("designated\t") else {
            return Err(format!(
                "state file {} line {} is malformed (expected a `designated\\t<id>` line)",
                path.display(),
                index + 2
            ));
        };
        if seen {
            return Err(format!(
                "state file {} names more than one designated strategy; refusing an \
                 ambiguous single-live record (SyRS SYS-2a)",
                path.display()
            ));
        }
        if id.trim().is_empty() {
            return Err(format!(
                "state file {} designates a blank strategy id",
                path.display()
            ));
        }
        let id = StrategyId::new(id.trim());
        let confirmation = LiveDesignationConfirmation::from_operator(
            id.clone(),
            "restored from the durable designation snapshot",
        )
        .map_err(|error| error.to_string())?;
        designation
            .designate(id, confirmation)
            .map_err(|error| error.to_string())?;
        seen = true;
    }
    Ok(designation)
}

/// Publish the designation durably: unique scratch file → fsync → atomic rename →
/// parent-directory fsync. The repo's durable-file pattern
/// (`orch005_rollback_cli::save_state`, `atp_simulation::backtest_store`).
/// Outcome of publishing the designation, split by whether the durable state
/// ALREADY CHANGED when the failure happened.
///
/// The distinction is load-bearing for the REST surface: a non-2xx there documents
/// "nothing mutated; retry is allowed". A failure AFTER the atomic rename has
/// already moved the live slot, so reporting it the same way would invite a retry
/// of a swap that already took effect.
pub enum PublishOutcome {
    /// Written and fsynced.
    Durable,
    /// Failed BEFORE the rename — the durable record is untouched.
    FailedBeforePublish(String),
    /// The rename SUCCEEDED (the next process will read the new designation) but a
    /// later step did not. The live slot has moved; only crash-durability is
    /// uncertain.
    PublishedNotSynced(String),
}

pub fn save_designation(path: &Path, designation: &LiveDesignation) -> PublishOutcome {
    let mut body = String::from(STATE_MAGIC);
    body.push('\n');
    if let Some(id) = designation.designated() {
        // Write-side validation is a SUPERSET of the loader's, so a successful
        // save can never produce a snapshot the next load refuses.
        if id.as_str().trim().is_empty() || id.as_str().contains(['\t', '\n']) {
            return PublishOutcome::FailedBeforePublish(format!(
                "designated strategy id {:?} would write a snapshot the loader refuses",
                id.as_str()
            ));
        }
        body.push_str(&format!("designated\t{}\n", id.as_str()));
    }
    let seq = SCRATCH_SEQ.fetch_add(1, Ordering::Relaxed);
    let scratch = path.with_extension(format!("tmp.{}.{seq}", std::process::id()));
    {
        let mut file = match fs::File::create(&scratch) {
            Ok(file) => file,
            Err(error) => {
                return PublishOutcome::FailedBeforePublish(format!(
                    "cannot create scratch {}: {error}",
                    scratch.display()
                ))
            }
        };
        if let Err(error) = file
            .write_all(body.as_bytes())
            .and_then(|()| file.sync_all())
        {
            let _ = fs::remove_file(&scratch);
            return PublishOutcome::FailedBeforePublish(format!(
                "cannot write scratch {}: {error}",
                scratch.display()
            ));
        }
    }
    if let Err(error) = fs::rename(&scratch, path) {
        let _ = fs::remove_file(&scratch);
        return PublishOutcome::FailedBeforePublish(format!(
            "cannot publish {} (rename): {error}",
            path.display()
        ));
    }
    // PAST THIS POINT the live slot has moved: the rename is atomic and the next
    // process reads the new designation. A failure here is NOT "nothing happened".
    let parent = path.parent().filter(|p| !p.as_os_str().is_empty());
    match fs::File::open(parent.unwrap_or_else(|| Path::new("."))).and_then(|dir| dir.sync_all()) {
        Ok(()) => PublishOutcome::Durable,
        Err(error) => {
            PublishOutcome::PublishedNotSynced(format!("cannot fsync state directory: {error}"))
        }
    }
}
