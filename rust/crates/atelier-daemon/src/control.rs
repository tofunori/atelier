//! Private Unix domain control protocol (JSON Lines).

use crate::{
    build_info::{BuildInfo, PROTOCOL_VERSION, RuntimeClock},
    config::DaemonConfig,
    host::ProjectHost,
    registry::ProjectRegistry,
    sessions::SessionStore,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    fs, io,
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
    pub registry: ProjectRegistry,
    pub host: ProjectHost,
    pub sessions: SessionStore,
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
                "projects": state.registry.project_count().await,
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
        "project.register" => {
            let Some(root) = request.params.get("root").and_then(Value::as_str) else {
                return error_response(request.id, "INVALID_PARAMS", "root required");
            };
            match state.registry.register(std::path::Path::new(root)).await {
                Ok((key, entry)) => ControlResponse {
                    id: request.id,
                    ok: true,
                    result: Some(json!({
                        "key": key,
                        "displayName": entry.display_name,
                        "state": "registeredIdle",
                        "canonicalRoot": entry.canonical_root,
                    })),
                    error: None,
                },
                Err(message) => error_response(request.id, "REGISTER_FAILED", message),
            }
        }
        "project.list" => ControlResponse {
            id: request.id,
            ok: true,
            result: Some(json!({ "projects": state.host.list_public().await })),
            error: None,
        },
        "project.status" => {
            let Some(key) = request
                .params
                .get("key")
                .and_then(Value::as_str)
                .map(str::to_string)
                .or_else(|| {
                    request
                        .params
                        .get("root")
                        .and_then(Value::as_str)
                        .and_then(|root| atelier_core::project_key(std::path::Path::new(root)).ok())
                })
            else {
                return error_response(request.id, "INVALID_PARAMS", "key or root required");
            };
            match state.host.status(&key).await {
                Some(status) => ControlResponse {
                    id: request.id,
                    ok: true,
                    result: Some(status),
                    error: None,
                },
                None => error_response(request.id, "PROJECT_NOT_FOUND", "project not found"),
            }
        }
        "project.open" => {
            let key = request
                .params
                .get("key")
                .and_then(Value::as_str)
                .map(str::to_string);
            let key = match key {
                Some(key) => key,
                None => {
                    let Some(root) = request.params.get("root").and_then(Value::as_str) else {
                        return error_response(
                            request.id,
                            "INVALID_PARAMS",
                            "key or root required",
                        );
                    };
                    match state.registry.register(std::path::Path::new(root)).await {
                        Ok((key, _)) => key,
                        Err(message) => {
                            return error_response(request.id, "REGISTER_FAILED", message);
                        }
                    }
                }
            };
            if let Err(error) = state.host.activate(&key).await {
                return error_response(request.id, error.code(), error.message());
            }
            let consumer = request
                .params
                .get("consumer")
                .and_then(Value::as_str)
                .map(str::to_string);
            let theme = request
                .params
                .get("theme")
                .and_then(Value::as_str)
                .map(str::to_string);
            let native_fs = request
                .params
                .get("nativeFs")
                .and_then(Value::as_bool)
                .unwrap_or(true);
            match state
                .sessions
                .mint_ticket(&key, consumer, theme.clone(), native_fs)
                .await
            {
                Ok(ticket) => {
                    let url = format!(
                        "http://{}:{}/p/{}/figures_index.html?nativeFs={}&theme={}",
                        state.config.host,
                        state.config.port,
                        key,
                        if native_fs { "1" } else { "0" },
                        theme.as_deref().unwrap_or("Codex")
                    );
                    let open_url = format!(
                        "http://{}:{}/open/{}",
                        state.config.host, state.config.port, ticket
                    );
                    ControlResponse {
                        id: request.id,
                        ok: true,
                        result: Some(json!({
                            "key": key,
                            "url": url,
                            "openUrl": open_url,
                        })),
                        error: None,
                    }
                }
                Err(message) => error_response(request.id, "TICKET_FAILED", message),
            }
        }

        "consumer.register" => {
            let Some(key) = request.params.get("key").and_then(Value::as_str) else {
                return error_response(request.id, "INVALID_PARAMS", "key required");
            };
            let thread = request
                .params
                .get("thread")
                .and_then(Value::as_str)
                .unwrap_or("consumer");
            let label = request
                .params
                .get("label")
                .and_then(Value::as_str)
                .unwrap_or("Codex task");
            let automatic = request
                .params
                .get("automatic")
                .and_then(Value::as_bool)
                .or_else(|| {
                    request
                        .params
                        .get("mode")
                        .and_then(Value::as_str)
                        .map(|mode| {
                            mode.eq_ignore_ascii_case("automatic")
                                || mode.eq_ignore_ascii_case("auto")
                        })
                })
                .unwrap_or(false);
            match state.host.activate(key).await {
                Ok(runtime) => {
                    let agent_store = runtime.agent();
                    let mut agent = agent_store.lock().await;
                    match agent.register(
                        format!("thread:{thread}"),
                        thread.to_string(),
                        Some(label.to_string()),
                        Some(thread.to_string()),
                        Some(automatic),
                        None,
                    ) {
                        Ok(value) => ControlResponse {
                            id: request.id,
                            ok: true,
                            result: Some(value),
                            error: None,
                        },
                        Err(message) => error_response(request.id, "REGISTER_FAILED", message),
                    }
                }
                Err(error) => error_response(request.id, error.code(), error.message()),
            }
        }
        // Claim pending annotations for a consumer (MCP atelier_get_selection).
        "annotation.claim" | "annotation.list" => {
            let Some(key) = request.params.get("key").and_then(Value::as_str) else {
                return error_response(request.id, "INVALID_PARAMS", "key required");
            };
            let consumer = request
                .params
                .get("consumer")
                .and_then(Value::as_str)
                .unwrap_or("");
            // Prefer explicit destination; otherwise thread:{consumer} (Codex convention).
            let destination = request
                .params
                .get("destination")
                .and_then(Value::as_str)
                .map(str::to_string)
                .unwrap_or_else(|| format!("thread:{consumer}"));
            match state.host.activate(key).await {
                Ok(runtime) => {
                    let agent_store = runtime.agent();
                    let mut agent = agent_store.lock().await;
                    match agent.claim(consumer, &destination) {
                        Ok(items) => {
                            let _ = state
                                .host
                                .publish(
                                    key,
                                    "annotation.changed",
                                    json!({"pending": items.len(), "op": "claim"}),
                                )
                                .await;
                            ControlResponse {
                                id: request.id,
                                ok: true,
                                result: Some(json!({
                                    "ok": true,
                                    "items": items,
                                    "consumer": consumer,
                                    "destination": destination,
                                })),
                                error: None,
                            }
                        }
                        Err(message) => error_response(request.id, "ANNOTATION_FAILED", message),
                    }
                }
                Err(error) => error_response(request.id, error.code(), error.message()),
            }
        }
        // Full bank view: pending + history + consumers (MCP atelier_list_annotations).
        "annotation.bank" => {
            let Some(key) = request.params.get("key").and_then(Value::as_str) else {
                return error_response(request.id, "INVALID_PARAMS", "key required");
            };
            let limit = request
                .params
                .get("limit")
                .and_then(Value::as_u64)
                .unwrap_or(50)
                .clamp(1, 200) as usize;
            match state.host.activate(key).await {
                Ok(runtime) => {
                    let agent_store = runtime.agent();
                    let agent = agent_store.lock().await;
                    ControlResponse {
                        id: request.id,
                        ok: true,
                        result: Some(agent.status(limit)),
                        error: None,
                    }
                }
                Err(error) => error_response(request.id, error.code(), error.message()),
            }
        }
        "annotation.ack" => {
            let Some(key) = request.params.get("key").and_then(Value::as_str) else {
                return error_response(request.id, "INVALID_PARAMS", "key required");
            };
            let consumer = request
                .params
                .get("consumer")
                .and_then(Value::as_str)
                .unwrap_or("");
            let ids: Vec<String> = request
                .params
                .get("ids")
                .and_then(Value::as_array)
                .map(|arr| {
                    arr.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default();
            match state.host.activate(key).await {
                Ok(runtime) => {
                    let agent_store = runtime.agent();
                    let mut agent = agent_store.lock().await;
                    match agent.acknowledge(&ids, consumer) {
                        Ok(count) => {
                            let _ = state
                                .host
                                .publish(
                                    key,
                                    "annotation.changed",
                                    json!({"acked": count, "op": "ack"}),
                                )
                                .await;
                            ControlResponse {
                                id: request.id,
                                ok: true,
                                result: Some(json!({ "ok": true, "acked": count, "ids": ids })),
                                error: None,
                            }
                        }
                        Err(message) => error_response(request.id, "ANNOTATION_FAILED", message),
                    }
                }
                Err(error) => error_response(request.id, error.code(), error.message()),
            }
        }
        // Mutate annotation lifecycle: processing | completed | failed | …
        "annotation.status" => {
            let Some(key) = request.params.get("key").and_then(Value::as_str) else {
                return error_response(request.id, "INVALID_PARAMS", "key required");
            };
            let ids: Vec<String> = request
                .params
                .get("ids")
                .and_then(Value::as_array)
                .map(|arr| {
                    arr.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default();
            let status = request
                .params
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("");
            let result = request
                .params
                .get("result")
                .and_then(Value::as_str)
                .unwrap_or("");
            let error = request
                .params
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("");
            if ids.is_empty() || status.is_empty() {
                return error_response(request.id, "INVALID_PARAMS", "ids and status are required");
            }
            match state.host.activate(key).await {
                Ok(runtime) => {
                    let agent_store = runtime.agent();
                    let mut agent = agent_store.lock().await;
                    match agent.update_status(&ids, status, result, error) {
                        Ok(changed) => {
                            let _ = state
                                .host
                                .publish(
                                    key,
                                    "annotation.changed",
                                    json!({"updated": changed, "status": status, "op": "status"}),
                                )
                                .await;
                            ControlResponse {
                                id: request.id,
                                ok: true,
                                result: Some(json!({
                                    "ok": true,
                                    "updated": changed,
                                    "ids": ids,
                                    "status": status,
                                })),
                                error: None,
                            }
                        }
                        Err(message) => error_response(request.id, "ANNOTATION_FAILED", message),
                    }
                }
                Err(error) => error_response(request.id, error.code(), error.message()),
            }
        }
        // Test/control helper: enqueue an annotation into the project bank.
        "annotation.enqueue" => {
            let Some(key) = request.params.get("key").and_then(Value::as_str) else {
                return error_response(request.id, "INVALID_PARAMS", "key required");
            };
            match state.host.activate(key).await {
                Ok(runtime) => {
                    let agent_store = runtime.agent();
                    let mut agent = agent_store.lock().await;
                    let mut payload = serde_json::Map::new();
                    payload.insert(
                        "artifact".into(),
                        request
                            .params
                            .get("artifact")
                            .cloned()
                            .unwrap_or(json!("notes.md")),
                    );
                    payload.insert(
                        "comment".into(),
                        request
                            .params
                            .get("comment")
                            .cloned()
                            .unwrap_or(json!("test annotation")),
                    );
                    payload.insert(
                        "destination".into(),
                        request
                            .params
                            .get("destination")
                            .cloned()
                            .unwrap_or(json!("auto")),
                    );
                    if let Some(held) = request.params.get("held") {
                        payload.insert("held".into(), held.clone());
                    }
                    match agent.enqueue_event(payload) {
                        Ok(event) => {
                            let _ = state
                                .host
                                .publish(
                                    key,
                                    "annotation.changed",
                                    json!({"op": "enqueue", "id": event.get("id")}),
                                )
                                .await;
                            ControlResponse {
                                id: request.id,
                                ok: true,
                                result: Some(event),
                                error: None,
                            }
                        }
                        Err(message) => error_response(request.id, "ANNOTATION_FAILED", message),
                    }
                }
                Err(error) => error_response(request.id, error.code(), error.message()),
            }
        }
        "project.rescan" => {
            let Some(key) = request.params.get("key").and_then(Value::as_str) else {
                return error_response(request.id, "INVALID_PARAMS", "key required");
            };
            match state.host.activate(key).await {
                Ok(runtime) => {
                    let root = runtime.root().to_path_buf();
                    let outcome = atelier_server::rebuild(
                        &root,
                        &runtime.watcher(),
                        &runtime.revision(),
                        &runtime.rebuild_lock(),
                    )
                    .await;
                    ControlResponse {
                        id: request.id,
                        ok: outcome.ok,
                        result: Some(json!({
                            "key": key,
                            "ok": outcome.ok,
                            "revision": *runtime.revision().read().await,
                            "message": outcome.out,
                        })),
                        error: if outcome.ok {
                            None
                        } else {
                            Some(ControlError {
                                code: "RESCAN_FAILED".into(),
                                message: outcome.out,
                            })
                        },
                    }
                }
                Err(error) => error_response(request.id, error.code(), error.message()),
            }
        }
        "project.event" => {
            let Some(key) = request.params.get("key").and_then(Value::as_str) else {
                return error_response(request.id, "INVALID_PARAMS", "key required");
            };
            match state.host.activate(key).await {
                Ok(_runtime) => ControlResponse {
                    id: request.id,
                    ok: true,
                    result: Some(json!({
                        "key": key,
                        "rel": request.params.get("rel"),
                        "note": request.params.get("note"),
                        "accepted": true,
                    })),
                    error: None,
                },
                Err(error) => error_response(request.id, error.code(), error.message()),
            }
        }
        "project.suspend" => {
            let Some(key) = request.params.get("key").and_then(Value::as_str) else {
                return error_response(request.id, "INVALID_PARAMS", "key required");
            };
            match state.host.suspend(key).await {
                Ok(()) => ControlResponse {
                    id: request.id,
                    ok: true,
                    result: Some(json!({ "key": key, "state": "suspended" })),
                    error: None,
                },
                Err(error) => error_response(request.id, error.code(), error.message()),
            }
        }
        "project.forget" => {
            let Some(key) = request.params.get("key").and_then(Value::as_str) else {
                return error_response(request.id, "INVALID_PARAMS", "key required");
            };
            let removed = state.host.forget(key).await;
            ControlResponse {
                id: request.id,
                ok: true,
                result: Some(json!({ "key": key, "removed": removed })),
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
