//! The host's sockets: one Unix domain socket per registered strategy.
//!
//! `<socket_dir>/<strategy_id>/order.sock` is the ONLY way a strategy reaches the
//! host, so the socket a connection arrives on IS the strategy's identity; the
//! frame never names one. A Unix socket also binds no TCP port, so parallel
//! worktrees and the IB ports (4001/4002) are never touched.
//!
//! # What makes "the socket is the identity" true
//!
//! What the host enforces itself:
//!
//! * `socket_dir` must be closed to group and other (`mode & 0o077 == 0`), or the
//!   host refuses to start. Only the host's user (and root) can reach any socket.
//! * Each strategy gets its OWN directory, `0700`, holding only its socket (`0600`).
//!   So a strategy container is given exactly one directory, and nothing inside it
//!   names another strategy's socket. A directory, not the socket file, because a
//!   bind mount of a file pins the inode: after a host restart re-creates the
//!   socket, a file mount would still point at the dead one.
//! * Strategy containers drop every capability (SRS-SEC-003, `cap_drop: ALL`), so
//!   root inside a container has no `CAP_DAC_OVERRIDE` to walk past these modes.
//!
//! What the host cannot enforce: WHICH directory the container runtime mounts into
//! which container. That is the concrete Docker `StrategyContainerRuntime`'s job
//! (mount `<socket_dir>/<id>/` into container `<id>` and nothing else), and it is
//! not built yet. Verifying the peer's credentials would need
//! `UnixStream::peer_cred`, which is not stable Rust. See
//! `live_designation_contract.deferred[]`.
//!
//! A second host over the same directory is refused: the first holds an OS file
//! lock on `<socket_dir>/host.lock` ([`std::fs::File::try_lock`]), which the kernel
//! releases if the host dies, so a crash never wedges the next start. Only while
//! holding that lock does a start remove the previous run's socket files.

use super::protocol::{self, Reply, MAX_FRAME_BYTES};
use super::LiveExecutionHost;
use atp_execution::{
    BrokerageConnectivity, ConnectivityEventSink, LiveBrokerageSubmit, MarketDataFreshnessProbe,
    StaleDataEventSink,
};
use atp_types::StrategyId;
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::{FileTypeExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::Duration;

/// Concurrent connections one strategy socket accepts. A strategy needs one; the
/// bound stops a misbehaving container from exhausting host threads.
pub const MAX_CONNECTIONS_PER_STRATEGY: usize = 4;

/// How long the host waits to write a reply before dropping the connection.
pub const REPLY_WRITE_TIMEOUT: Duration = Duration::from_secs(5);

/// The longest socket path the host binds. `sun_path` is 104 bytes on macOS and 108
/// on Linux, including the terminating NUL; the smaller bound is portable.
pub const MAX_SOCKET_PATH_BYTES: usize = 103;

/// The longest strategy id the host serves (it becomes a file name).
pub const MAX_STRATEGY_ID_LEN: usize = 64;

/// Why the sockets could not be opened.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServeError {
    pub reason: String,
}

impl fmt::Display for ServeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "SRS-EXE-001 live execution host cannot serve: {}",
            self.reason
        )
    }
}

impl std::error::Error for ServeError {}

fn serve_error(reason: impl Into<String>) -> ServeError {
    ServeError {
        reason: reason.into(),
    }
}

/// A strategy id becomes a socket file name, so it is restricted to a portable,
/// separator-free alphabet: `[A-Za-z0-9][A-Za-z0-9._-]*`, at most 64 bytes.
pub fn validate_strategy_id(id: &str) -> Result<(), ServeError> {
    let mut chars = id.chars();
    let first_ok = chars.next().is_some_and(|c| c.is_ascii_alphanumeric());
    let rest_ok = chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'));
    if !first_ok || !rest_ok || id.len() > MAX_STRATEGY_ID_LEN {
        return Err(serve_error(format!(
            "strategy id {id:?} must match [A-Za-z0-9][A-Za-z0-9._-]* and be at most \
             {MAX_STRATEGY_ID_LEN} bytes (it names a socket file)"
        )));
    }
    Ok(())
}

/// The file name of every strategy's socket inside its own directory.
pub const SOCKET_FILE_NAME: &str = "order.sock";

/// The per-strategy directory: the one thing a strategy's container is given.
pub fn strategy_dir(socket_dir: &Path, strategy: &str) -> PathBuf {
    socket_dir.join(strategy)
}

/// The socket path for `strategy` under `socket_dir`.
pub fn socket_path(socket_dir: &Path, strategy: &str) -> PathBuf {
    strategy_dir(socket_dir, strategy).join(SOCKET_FILE_NAME)
}

/// The bound sockets. Dropping this does not stop the accept threads; the host
/// runs until its process exits.
pub struct BoundHost {
    _instance_lock: File,
    sockets: Vec<PathBuf>,
}

impl BoundHost {
    /// The socket files this host is serving, one per strategy, in argument order.
    pub fn sockets(&self) -> &[PathBuf] {
        &self.sockets
    }
}

/// Bind one socket per strategy and start serving `host` on background threads.
///
/// Refuses an empty or duplicated strategy list, an invalid strategy id, a missing
/// socket directory, a second host over the same directory, and a non-socket file
/// squatting on a socket path.
pub fn bind<B, C, E, F, S>(
    host: Arc<LiveExecutionHost<B, C, E, F, S>>,
    socket_dir: &Path,
    strategies: &[String],
) -> Result<(BoundHost, Vec<JoinHandle<()>>), ServeError>
where
    B: LiveBrokerageSubmit + Send + 'static,
    C: BrokerageConnectivity + Send + 'static,
    E: ConnectivityEventSink + Send + 'static,
    F: MarketDataFreshnessProbe + Send + 'static,
    S: StaleDataEventSink + Send + 'static,
{
    if strategies.is_empty() {
        return Err(serve_error(
            "no --strategy was given; there is nobody to serve",
        ));
    }
    for (index, id) in strategies.iter().enumerate() {
        validate_strategy_id(id)?;
        if strategies[..index].contains(id) {
            return Err(serve_error(format!("strategy {id:?} is listed twice")));
        }
    }
    let dir_meta = fs::symlink_metadata(socket_dir).map_err(|error| {
        serve_error(format!(
            "socket directory {} cannot be read: {error}",
            socket_dir.display()
        ))
    })?;
    if !dir_meta.is_dir() {
        return Err(serve_error(format!(
            "socket directory {} is not a directory (a symlink is refused too)",
            socket_dir.display()
        )));
    }
    let mode = dir_meta.permissions().mode() & 0o777;
    if mode & 0o077 != 0 {
        return Err(serve_error(format!(
            "socket directory {} has mode {mode:04o}; it must be closed to group and other \
             (e.g. 0700), or any local user could reach the live strategy's socket",
            socket_dir.display()
        )));
    }

    let lock_path = socket_dir.join("host.lock");
    let instance_lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(&lock_path)
        .map_err(|error| serve_error(format!("cannot open {}: {error}", lock_path.display())))?;
    instance_lock.try_lock().map_err(|_| {
        serve_error(format!(
            "another live execution host already serves {} (it holds {})",
            socket_dir.display(),
            lock_path.display()
        ))
    })?;

    let mut listeners = Vec::with_capacity(strategies.len());
    for id in strategies {
        prepare_strategy_dir(&strategy_dir(socket_dir, id))?;
        let path = socket_path(socket_dir, id);
        if path.as_os_str().len() > MAX_SOCKET_PATH_BYTES {
            return Err(serve_error(format!(
                "socket path {} is {} bytes; Unix socket paths must be at most \
                 {MAX_SOCKET_PATH_BYTES} (use a shorter --socket-dir)",
                path.display(),
                path.as_os_str().len()
            )));
        }
        match fs::symlink_metadata(&path) {
            Ok(meta) if meta.file_type().is_socket() => {
                fs::remove_file(&path).map_err(|error| {
                    serve_error(format!(
                        "cannot remove stale socket {}: {error}",
                        path.display()
                    ))
                })?
            }
            Ok(_) => {
                return Err(serve_error(format!(
                    "{} exists and is not a socket; refusing to replace it",
                    path.display()
                )))
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(serve_error(format!(
                    "cannot inspect {}: {error}",
                    path.display()
                )))
            }
        }
        let listener = UnixListener::bind(&path)
            .map_err(|error| serve_error(format!("cannot bind {}: {error}", path.display())))?;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
            .map_err(|error| serve_error(format!("cannot restrict {}: {error}", path.display())))?;
        listeners.push((StrategyId::new(id.as_str()), path, listener));
    }

    let sockets = listeners.iter().map(|(_, path, _)| path.clone()).collect();
    let handles = listeners
        .into_iter()
        .map(|(strategy, _, listener)| {
            let host = Arc::clone(&host);
            std::thread::spawn(move || accept_loop(host, strategy, listener))
        })
        .collect();
    Ok((
        BoundHost {
            _instance_lock: instance_lock,
            sockets,
        },
        handles,
    ))
}

/// Create (or re-use) one strategy's directory and close it to everyone but the
/// host's user. An existing entry must be a real directory: a symlink could point
/// the socket somewhere another strategy can reach.
fn prepare_strategy_dir(dir: &Path) -> Result<(), ServeError> {
    match fs::symlink_metadata(dir) {
        Ok(meta) if meta.is_dir() => {}
        Ok(_) => {
            return Err(serve_error(format!(
                "{} exists and is not a directory; refusing to use it",
                dir.display()
            )))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            fs::create_dir(dir).map_err(|error| {
                serve_error(format!("cannot create {}: {error}", dir.display()))
            })?;
        }
        Err(error) => {
            return Err(serve_error(format!(
                "cannot inspect {}: {error}",
                dir.display()
            )))
        }
    }
    fs::set_permissions(dir, fs::Permissions::from_mode(0o700))
        .map_err(|error| serve_error(format!("cannot restrict {}: {error}", dir.display())))
}

fn accept_loop<B, C, E, F, S>(
    host: Arc<LiveExecutionHost<B, C, E, F, S>>,
    strategy: StrategyId,
    listener: UnixListener,
) where
    B: LiveBrokerageSubmit + Send + 'static,
    C: BrokerageConnectivity + Send + 'static,
    E: ConnectivityEventSink + Send + 'static,
    F: MarketDataFreshnessProbe + Send + 'static,
    S: StaleDataEventSink + Send + 'static,
{
    let open = Arc::new(AtomicUsize::new(0));
    for stream in listener.incoming() {
        let Ok(mut stream) = stream else { continue };
        if open.fetch_add(1, Ordering::SeqCst) >= MAX_CONNECTIONS_PER_STRATEGY {
            open.fetch_sub(1, Ordering::SeqCst);
            let refusal = host_refusal(
                &host,
                "TooManyConnections",
                format!(
                    "strategy `{}` already holds {MAX_CONNECTIONS_PER_STRATEGY} connections",
                    strategy.as_str()
                ),
            );
            let _ = write_reply(&mut stream, &refusal);
            continue;
        }
        let host = Arc::clone(&host);
        let strategy = strategy.clone();
        let open = Arc::clone(&open);
        std::thread::spawn(move || {
            serve_connection(&host, &strategy, stream);
            open.fetch_sub(1, Ordering::SeqCst);
        });
    }
}

fn host_refusal<B, C, E, F, S>(
    host: &LiveExecutionHost<B, C, E, F, S>,
    error_type: &str,
    message: String,
) -> Reply
where
    B: LiveBrokerageSubmit,
    C: BrokerageConnectivity,
    E: ConnectivityEventSink,
    F: MarketDataFreshnessProbe,
    S: StaleDataEventSink,
{
    Reply::Refused {
        correlation_id: None,
        error_type: error_type.to_string(),
        message,
        tier: host.tier().as_str(),
    }
}

/// Serve request frames on one connection until the client closes it or breaks
/// the protocol. An oversize or non-UTF-8 frame gets one refusal and the
/// connection is closed, since the rest of the byte stream can no longer be framed.
fn serve_connection<B, C, E, F, S>(
    host: &LiveExecutionHost<B, C, E, F, S>,
    strategy: &StrategyId,
    stream: UnixStream,
) where
    B: LiveBrokerageSubmit,
    C: BrokerageConnectivity,
    E: ConnectivityEventSink,
    F: MarketDataFreshnessProbe,
    S: StaleDataEventSink,
{
    let Ok(mut writer) = stream.try_clone() else {
        return;
    };
    if writer.set_write_timeout(Some(REPLY_WRITE_TIMEOUT)).is_err() {
        return;
    }
    let mut reader = BufReader::new(stream);
    loop {
        let mut buffer = Vec::new();
        // +1 for the newline, +1 more to detect an over-long frame.
        let limit = (MAX_FRAME_BYTES + 2) as u64;
        match reader.by_ref().take(limit).read_until(b'\n', &mut buffer) {
            Ok(0) | Err(_) => return,
            Ok(_) => {}
        }
        let terminated = buffer.last() == Some(&b'\n');
        if terminated {
            buffer.pop();
        }
        if !terminated || buffer.len() > MAX_FRAME_BYTES {
            let refusal = host_refusal(
                host,
                "ProtocolError",
                format!("frame is not newline-terminated within {MAX_FRAME_BYTES} bytes; closing"),
            );
            let _ = write_reply(&mut writer, &refusal);
            return;
        }
        let reply = match std::str::from_utf8(&buffer) {
            Ok(frame) => host.handle_frame(strategy, frame),
            Err(_) => {
                let refusal = host_refusal(host, "ProtocolError", "frame is not UTF-8".into());
                let _ = write_reply(&mut writer, &refusal);
                return;
            }
        };
        if write_reply(&mut writer, &reply).is_err() {
            return;
        }
    }
}

fn write_reply(stream: &mut UnixStream, reply: &Reply) -> std::io::Result<()> {
    let mut frame = protocol::encode_reply(reply);
    frame.push('\n');
    stream.write_all(frame.as_bytes())?;
    stream.flush()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strategy_ids_that_could_escape_the_socket_directory_are_refused() {
        for bad in [
            "",
            "../live",
            "a/b",
            ".hidden",
            "-x",
            "a b",
            "a\tb",
            &"x".repeat(65),
        ] {
            assert!(validate_strategy_id(bad).is_err(), "{bad:?}");
        }
        for good in ["live-a", "paper.1", "P_2", &"x".repeat(64)] {
            assert!(validate_strategy_id(good).is_ok(), "{good:?}");
        }
    }
}
