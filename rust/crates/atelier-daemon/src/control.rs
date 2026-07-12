//! Private Unix domain control protocol (JSON Lines).

use crate::{
    build_info::{BuildInfo, PROTOCOL_VERSION, RuntimeClock},
    config::DaemonConfig,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    fs,
    io,
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::{UnixListener, UnixStream},
    sync::Notify,
};

#[cfg(unix)]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};

#[derive(Debug, Deserialize)]
pub struct ControlRequest {
    pub id: String,
    pub protocol: u32,
    pub method: String,
    #[serde(default)]
    pub params: Value,
    /// Optional bearer token; required when the control token file exists.
    #[serde(default)]
    pub token: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct ControlResponse {
    pub id: String,
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<ControlError>,
}

#[derive(Debug, Serialize)]
pub struct ControlError {
    pub code: String,
    pub message: String,
}

#[derive(Clone)]
pub struct ControlState {
    pub config: Arc<DaemonConfig>,
    pub build: Arc<BuildInfo>,
    pub clock: Arc<RuntimeClock>,
    pub token: Arc<String>,
    pub shutting_down: Arc<AtomicBool>,
    pub shutdown: Arc<Notify>,
}

pub fn ensure_private_state_dir(state_dir: &Path) -> io::Result<()> {
    fs::create_dir_all(state_dir)?;
    #[cfg(unix)]
    {
        let perms = fs::Permissions::from_mode(0o700);
        fs::set_permissions(state_dir, perms)?;
        let logs = state_dir.join("logs");
        fs::create_dir_all(&logs)?;
        fs::set_permissions(&logs, fs::Permissions::from_mode(0o700))?;
        let projects = state_dir.join("projects");
        fs::create_dir_all(&projects)?;
        fs::set_permissions(&projects, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

pub fn load_or_create_token(path: &Path) -> io::Result<String> {
    if path.is_file() {
        let token = fs::read_to_string(path)?;
        let token = token.trim().to_string();
        if !token.is_empty() {
            return Ok(token);
        }
    }
    let token = hex::encode(uuid::Uuid::new_v4().as_bytes())
        + &hex::encode(uuid::Uuid::new_v4().as_bytes());
    write_private_file(path, token.as_bytes())?;
    Ok(token)
}

pub fn write_private_file(path: &Path, bytes: &[u8]) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut options = fs::OpenOptions::new();
    options.create(true).write(true).truncate(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options.open(path)?;
    use std::io::Write;
    file.write_all(bytes)?;
    file.sync_all()?;
    #[cfg(unix)]
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    Ok(())
}

pub async fn serve_control(listener: UnixListener, state: ControlState) {
    loop {
        if state.shutting_down.load(Ordering::SeqCst) {
            break;
        }
        match listener.accept().await {
            Ok((stream, _addr)) => {
                let state = state.clone();
                tokio::spawn(async move {
                    if let Err(error) = handle_connection(stream, state).await {
                        tracing::debug!("control connection ended: {error}");
                    }
                });
            }
            Err(error) => {
                if state.shutting_down.load(Ordering::SeqCst) {
                    break;
                }
                tracing::warn!("control accept failed: {error}");
            }
        }
    }
}

async fn handle_connection(stream: UnixStream, state: ControlState) -> io::Result<()> {
    let (reader, mut writer) = stream.into_split();
    let mut lines = BufReader::new(reader).lines();
    while let Some(line) = lines.next_line().await? {
        if line.trim().is_empty() {
            continue;
        }
        let response = match serde_json::from_str::<ControlRequest>(&line) {
            Ok(request) => dispatch(request, &state).await,
            Err(error) => ControlResponse {
                id: "invalid".into(),
                ok: false,
                result: None,
                error: Some(ControlError {
                    code: "INVALID_REQUEST".into(),
                    message: error.to_string(),
                }),
            },
        };
        let mut payload = serde_json::to_vec(&response).unwrap_or_else(|_| {
            br#"{"id":"invalid","ok":false,"error":{"code":"ENCODE","message":"failed"}}"#.to_vec()
        });
        payload.push(b'\n');
        writer.write_all(&payload).await?;
        if response.ok
            && response
                .result
                .as_ref()
                .and_then(|value| value.get("shuttingDown"))
                .and_then(Value::as_bool)
                == Some(true)
        {
            break;
        }
    }
    Ok(())
}

async fn dispatch(request: ControlRequest, state: &ControlState) -> ControlResponse {
    if request.protocol != PROTOCOL_VERSION {
        return error_response(
            request.id,
            "VERSION_MISMATCH",
            format!(
                "protocol {} unsupported; daemon speaks {}",
                request.protocol, PROTOCOL_VERSION
            ),
        );
    }
    if request.token.as_deref() != Some(state.token.as_str()) {
        return error_response(request.id, "UNAUTHORIZED", "invalid control token");
    }
    match request.method.as_str() {
        "daemon.health" => ControlResponse {
            id: request.id,
            ok: true,
            result: Some(json!({
                "pid": std::process::id(),
                "uptimeSecs": state.clock.uptime_secs(),
                "version": state.build.version,
                "gitSha": state.build.git_sha,
                "buildTimestamp": state.build.build_timestamp,
                "protocolVersion": state.build.protocol_version,
                "daemonInstance": state.build.daemon_instance,
                "assetHash": state.build.asset_hash,
                "host": state.config.host,
                "port": state.config.port,
                "stateDir": state.config.state_dir,
                "projects": 0,
            })),
            error: None,
        },
        "daemon.shutdown" => {
            let reason = request
                .params
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("control");
            tracing::info!(reason, "daemon.shutdown requested");
            state.shutting_down.store(true, Ordering::SeqCst);
            state.shutdown.notify_waiters();
            ControlResponse {
                id: request.id,
                ok: true,
                result: Some(json!({ "shuttingDown": true, "reason": reason })),
                error: None,
            }
        }
        other => error_response(
            request.id,
            "METHOD_NOT_FOUND",
            format!("unknown method: {other}"),
        ),
    }
}

fn error_response(id: String, code: &str, message: impl Into<String>) -> ControlResponse {
    ControlResponse {
        id,
        ok: false,
        result: None,
        error: Some(ControlError {
            code: code.into(),
            message: message.into(),
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn state_dir_is_private() {
        let dir = tempdir().unwrap();
        let state = dir.path().join("daemon");
        ensure_private_state_dir(&state).unwrap();
        #[cfg(unix)]
        {
            let mode = fs::metadata(&state).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o700);
        }
        let token_path = state.join("daemon.token");
        let token = load_or_create_token(&token_path).unwrap();
        assert_eq!(token.len(), 64);
        #[cfg(unix)]
        {
            let mode = fs::metadata(&token_path).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o600);
        }
    }
}
