//! Exclusive instance lock and safe control-socket takeover.

use crate::build_info::PROTOCOL_VERSION;
use serde_json::{Value, json};
use std::{
    fs::{self, File, OpenOptions},
    io::{self, BufRead, BufReader, Write},
    path::{Path, PathBuf},
    time::Duration,
};
use tokio::net::UnixListener;

#[cfg(unix)]
use std::os::unix::{fs::OpenOptionsExt, io::AsRawFd, net::UnixStream};

/// Process-held exclusive lock for one daemon state directory.
pub struct InstanceLock {
    _file: File,
    #[allow(dead_code)]
    path: PathBuf,
}

/// Acquire an exclusive non-blocking lock on `state_dir/daemon.lock`.
///
/// Returns `AlreadyRunning` if another process holds the lock.
pub fn acquire_instance_lock(state_dir: &Path) -> Result<InstanceLock, InstanceError> {
    fs::create_dir_all(state_dir).map_err(InstanceError::Io)?;
    let path = state_dir.join("daemon.lock");
    let mut options = OpenOptions::new();
    options.create(true).read(true).write(true);
    #[cfg(unix)]
    options.mode(0o600);
    let file = options.open(&path).map_err(InstanceError::Io)?;
    #[cfg(unix)]
    {
        let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
        if rc != 0 {
            return Err(InstanceError::AlreadyRunning {
                reason: "another atelier-daemon holds daemon.lock".into(),
            });
        }
        // Record our PID for diagnostics (best-effort).
        let _ = write_pid_locked(&file);
    }
    Ok(InstanceLock { _file: file, path })
}

#[cfg(unix)]
fn write_pid_locked(file: &File) -> io::Result<()> {
    use std::io::Seek;
    let mut file = file.try_clone()?;
    file.set_len(0)?;
    file.seek(io::SeekFrom::Start(0))?;
    writeln!(file, "{}", std::process::id())?;
    file.sync_all()?;
    Ok(())
}

#[derive(Debug)]
pub enum InstanceError {
    AlreadyRunning { reason: String },
    Io(io::Error),
    Other(String),
}

impl std::fmt::Display for InstanceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::AlreadyRunning { reason } => write!(f, "already running: {reason}"),
            Self::Io(error) => write!(f, "{error}"),
            Self::Other(message) => write!(f, "{message}"),
        }
    }
}

impl std::error::Error for InstanceError {}

/// Probe whether a live daemon answers on the control socket.
pub fn probe_live_control(socket_path: &Path, token: &str) -> Option<Value> {
    #[cfg(unix)]
    {
        let mut stream = UnixStream::connect(socket_path).ok()?;
        stream.set_read_timeout(Some(Duration::from_millis(400))).ok()?;
        stream.set_write_timeout(Some(Duration::from_millis(400))).ok()?;
        let req = json!({
            "id": "probe",
            "protocol": PROTOCOL_VERSION,
            "method": "daemon.health",
            "params": {},
            "token": token,
        });
        let mut line = req.to_string();
        line.push('\n');
        stream.write_all(line.as_bytes()).ok()?;
        let mut reader = BufReader::new(stream);
        let mut response = String::new();
        reader.read_line(&mut response).ok()?;
        let value: Value = serde_json::from_str(&response).ok()?;
        if value.get("ok") == Some(&json!(true)) {
            return Some(value.get("result").cloned().unwrap_or(value));
        }
        None
    }
    #[cfg(not(unix))]
    {
        let _ = (socket_path, token);
        None
    }
}

/// Probe HTTP /healthz on host:port without mutating state.
pub fn probe_http_health(host: &str, port: u16) -> bool {
    use std::net::TcpStream;
    let Ok(mut stream) = TcpStream::connect((host, port)) else {
        return false;
    };
    stream.set_read_timeout(Some(Duration::from_millis(400))).ok();
    let req = format!("GET /healthz HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n");
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = String::new();
    let _ = stream.read_to_string(&mut buf);
    buf.contains("\"ok\":true") && buf.contains("atelier-daemon")
}

/// Bind the control socket safely:
/// 1. if a live peer answers on the socket, refuse;
/// 2. remove only a dead/stale socket path;
/// 3. bind our listener.
pub fn bind_control_socket(
    socket_path: &Path,
    token: &str,
) -> Result<UnixListener, InstanceError> {
    if socket_path.exists() {
        if let Some(health) = probe_live_control(socket_path, token) {
            let pid = health
                .get("pid")
                .and_then(Value::as_u64)
                .unwrap_or_default();
            return Err(InstanceError::AlreadyRunning {
                reason: format!(
                    "live control socket at {} (pid {pid})",
                    socket_path.display()
                ),
            });
        }
        // Stale socket only — never remove a live peer's socket.
        let _ = fs::remove_file(socket_path);
    }
    let listener = UnixListener::bind(socket_path).map_err(|error| {
        InstanceError::Other(format!(
            "failed to bind control socket {}: {error}",
            socket_path.display()
        ))
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(socket_path, fs::Permissions::from_mode(0o600));
    }
    Ok(listener)
}

/// Ensure the HTTP port is free *before* taking over sockets.
pub fn ensure_http_port_free(host: &str, port: u16) -> Result<(), InstanceError> {
    if probe_http_health(host, port) {
        return Err(InstanceError::AlreadyRunning {
            reason: format!("HTTP {host}:{port} already serves atelier-daemon"),
        });
    }
    // Try a brief exclusive bind probe.
    match std::net::TcpListener::bind((host, port)) {
        Ok(listener) => {
            drop(listener);
            Ok(())
        }
        Err(error) if error.kind() == io::ErrorKind::AddrInUse => Err(InstanceError::AlreadyRunning {
            reason: format!("port {port} already in use on {host}"),
        }),
        Err(error) => Err(InstanceError::Io(error)),
    }
}

use std::io::Read;
