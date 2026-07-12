use atelier_core::{Health, WatcherStatus, is_artifact, is_excluded_dir};
use axum::{
    Json, Router,
    body::Body,
    extract::{Query, Request, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode, Uri, header},
    middleware::{self, Next},
    response::IntoResponse,
    routing::{get, post},
};
use base64::{Engine as _, engine::general_purpose::STANDARD as BASE64};
use notify::{Event, EventKind, RecursiveMode, Watcher};
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{
    process::Command,
    sync::{Mutex, RwLock, mpsc},
    time::sleep,
};
use tower_http::trace::TraceLayer;

use crate::project_runtime::ProjectRuntime;

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn relevant_change(root: &PathBuf, path: &std::path::Path) -> Option<String> {
    let relative = path.strip_prefix(root).ok()?;
    if path.file_name().and_then(|name| name.to_str()) == Some("figures_index.html") {
        return None;
    }
    if relative.components().any(|component| {
        matches!(component, std::path::Component::Normal(name)
            if is_excluded_dir(&name.to_string_lossy()))
    }) {
        return None;
    }
    if !is_artifact(path) {
        return None;
    }
    Some(relative.to_string_lossy().replace('\\', "/"))
}

async fn ping(State(state): State<ProjectRuntime>) -> Json<Health> {
    let codex = (!state.agent_token.is_empty()).then_some("codex".to_string());
    Json(Health {
        ok: true,
        service: "atelier",
        backend: "rust",
        project: state.root.to_string_lossy().to_string(),
        revision: *state.revision.read().await,
        watcher: state.watcher.read().await.clone(),
        agent_host: codex,
        agent_bridge_protocol: (!state.agent_token.is_empty()).then_some(2),
        agent_inbox: Some(state.agent.lock().await.pending_count()),
    })
}

async fn health(State(state): State<ProjectRuntime>) -> Json<Value> {
    let rebuild_busy = state.rebuild_lock.try_lock().is_err();
    let payload = serde_json::json!({
        "ok": true,
        "backend": "rust",
        "project": state.root,
        "revision": *state.revision.read().await,
        "watcher": state.watcher.read().await.clone(),
        "tasks": {
            "rebuildBusy": rebuild_busy,
            "thumbPermits": state.thumb_sem.available_permits(),
            "chromePermits": state.chrome_sem.available_permits(),
            "toastEvents": state.events.lock().await.len(),
            "boardQueue": state.board.lock().await.len(),
        },
    });
    Json(payload)
}

async fn revision(State(state): State<ProjectRuntime>) -> Json<Value> {
    Json(serde_json::json!({ "rev": *state.revision.read().await }))
}

async fn data(State(state): State<ProjectRuntime>) -> impl IntoResponse {
    let path = state.root.join("figures_data.json");
    match tokio::fs::read(path).await {
        Ok(bytes) => (
            StatusCode::OK,
            [
                ("content-type", "application/json"),
                ("cache-control", "no-cache"),
            ],
            bytes,
        )
            .into_response(),
        Err(error) => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({ "error": error.to_string() })),
        )
            .into_response(),
    }
}

fn default_gallery_state() -> Value {
    json!({
        "favs": [], "ratings": {}, "hidden": [], "tags": {},
        "hideRules": [], "collections": {}, "workflow": {}
    })
}

async fn gallery_state(State(state): State<ProjectRuntime>) -> impl IntoResponse {
    let path = state.root.join(".fig_state.json");
    match tokio::fs::read_to_string(path).await {
        Ok(raw) => match serde_json::from_str::<Value>(&raw) {
            Ok(value) => (StatusCode::OK, Json(value)).into_response(),
            Err(_) => (
                StatusCode::BAD_REQUEST,
                Json(json!({"error":"invalid gallery state"})),
            )
                .into_response(),
        },
        Err(_) => (StatusCode::OK, Json(default_gallery_state())).into_response(),
    }
}

async fn save_gallery_state(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
    Json(value): Json<Value>,
) -> impl IntoResponse {
    if !request_allowed(&headers, &state) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"loopback origin required"})),
        )
            .into_response();
    }
    if !value.is_object() {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error":"state must be an object"})),
        )
            .into_response();
    }
    if serde_json::to_vec(&value)
        .map(|bytes| bytes.len())
        .unwrap_or(usize::MAX)
        > 8 * 1024 * 1024
    {
        return (
            StatusCode::PAYLOAD_TOO_LARGE,
            Json(json!({"error":"state is too large"})),
        )
            .into_response();
    }
    let sanitized = sanitize_gallery_state(&value);
    let counts = json!({
        "ok": true,
        "favs": sanitized["favs"].as_array().map(Vec::len).unwrap_or(0),
        "ratings": sanitized["ratings"].as_object().map(serde_json::Map::len).unwrap_or(0),
        "hidden": sanitized["hidden"].as_array().map(Vec::len).unwrap_or(0),
    });
    match atelier_core::atomic_write_json(&state.root.join(".fig_state.json"), &sanitized) {
        Ok(()) => (StatusCode::OK, Json(counts)).into_response(),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error":error.to_string()})),
        )
            .into_response(),
    }
}

/// Réplique la sanitisation du POST /state Python : mêmes troncatures,
/// mêmes tris, mêmes valeurs autorisées — la validation vit côté serveur.
fn sanitize_gallery_state(request: &Value) -> Value {
    fn scalar(value: &Value) -> String {
        match value {
            Value::String(text) => text.clone(),
            other => other.to_string(),
        }
    }
    fn sorted_strings(value: Option<&Value>) -> Vec<String> {
        let Some(list) = value.and_then(Value::as_array) else {
            return Vec::new();
        };
        let set: std::collections::BTreeSet<String> = list.iter().map(scalar).collect();
        set.into_iter().collect()
    }

    let favs = sorted_strings(request.get("favs"));
    let hidden = sorted_strings(request.get("hidden"));

    let mut ratings = serde_json::Map::new();
    if let Some(map) = request.get("ratings").and_then(Value::as_object) {
        for (key, value) in map {
            if let Some(score) = value.as_i64()
                && (1..=5).contains(&score)
            {
                ratings.insert(key.clone(), json!(score));
            }
        }
    }

    let mut tags = serde_json::Map::new();
    if let Some(map) = request.get("tags").and_then(Value::as_object) {
        for (key, value) in map {
            let Some(list) = value.as_array().filter(|list| !list.is_empty()) else {
                continue;
            };
            let clean: Vec<String> = list
                .iter()
                .map(|tag| scalar(tag).trim().to_string())
                .filter(|tag| !tag.is_empty())
                .collect::<std::collections::BTreeSet<_>>()
                .into_iter()
                .take(30)
                .collect();
            if !clean.is_empty() {
                tags.insert(key.clone(), json!(clean));
            }
        }
    }

    let hide_rules: Vec<String> = request
        .get("hideRules")
        .and_then(Value::as_array)
        .map(|list| {
            list.iter()
                .filter_map(Value::as_str)
                .map(|rule| rule.trim().to_string())
                .filter(|rule| !rule.is_empty())
                .collect::<std::collections::BTreeSet<_>>()
                .into_iter()
                .take(200)
                .collect()
        })
        .unwrap_or_default();

    let mut collections = serde_json::Map::new();
    if let Some(map) = request.get("collections").and_then(Value::as_object) {
        for (key, value) in map {
            let name: String = key.trim().chars().take(80).collect();
            let Some(list) = value.as_array() else {
                continue;
            };
            if name.is_empty() {
                continue;
            }
            let clean: Vec<String> = list
                .iter()
                .filter_map(Value::as_str)
                .filter(|rel| !rel.trim().is_empty())
                .map(str::to_string)
                .collect::<std::collections::BTreeSet<_>>()
                .into_iter()
                .take(1000)
                .collect();
            collections.insert(name, json!(clean));
        }
    }

    let mut workflow = serde_json::Map::new();
    if let Some(map) = request.get("workflow").and_then(Value::as_object) {
        const ALLOWED: [&str; 4] = ["draft", "candidate", "final", "rejected"];
        for (key, value) in map {
            if let Some(status) = value.as_str()
                && ALLOWED.contains(&status)
            {
                workflow.insert(key.clone(), json!(status));
            }
        }
    }

    json!({
        "favs": favs,
        "ratings": ratings,
        "hidden": hidden,
        "tags": tags,
        "hideRules": hide_rules,
        "collections": collections,
        "workflow": workflow,
    })
}

#[derive(serde::Deserialize, Default)]
struct AgentStatusQuery {
    limit: Option<usize>,
}

async fn agent_status(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
    Query(query): Query<AgentStatusQuery>,
) -> impl IntoResponse {
    if !request_allowed(&headers, &state) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"loopback origin required"})),
        )
            .into_response();
    }
    let limit = query.limit.unwrap_or(50).clamp(1, 200);
    let mut payload = state.agent.lock().await.status(limit);
    if state.agent_token.is_empty() {
        payload["agentHost"] = Value::Null;
    }
    (StatusCode::OK, Json(payload)).into_response()
}

#[derive(serde::Deserialize)]
struct ProvenanceQuery {
    rel: String,
}

#[derive(serde::Deserialize)]
struct RegenerateRequest {
    rel: String,
}

async fn provenance(
    State(state): State<ProjectRuntime>,
    Query(query): Query<ProvenanceQuery>,
) -> impl IntoResponse {
    let path = state.root.join("figures_data.json");
    let Ok(raw) = tokio::fs::read_to_string(path).await else {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error":"figures_data.json not found"})),
        )
            .into_response();
    };
    let Ok(data) = serde_json::from_str::<Value>(&raw) else {
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error":"invalid figures_data.json"})),
        )
            .into_response();
    };
    let item = data
        .get("files")
        .and_then(Value::as_array)
        .and_then(|files| {
            files
                .iter()
                .find(|file| file.get("rel").and_then(Value::as_str) == Some(query.rel.as_str()))
        });
    match item {
        Some(item) => (
            StatusCode::OK,
            Json(json!({"ok":true,"rel":query.rel,"provenance":item.get("provenance")})),
        )
            .into_response(),
        None => (
            StatusCode::NOT_FOUND,
            Json(json!({"error":"artifact not found"})),
        )
            .into_response(),
    }
}

async fn regenerate(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
    Json(request): Json<RegenerateRequest>,
) -> impl IntoResponse {
    if !request_allowed(&headers, &state) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"loopback origin required"})),
        )
            .into_response();
    }
    let data_path = state.root.join("figures_data.json");
    let Ok(raw) = tokio::fs::read_to_string(data_path).await else {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error":"figures_data.json not found"})),
        )
            .into_response();
    };
    let Ok(data) = serde_json::from_str::<Value>(&raw) else {
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error":"invalid figures_data.json"})),
        )
            .into_response();
    };
    let Some(command) = data
        .get("files")
        .and_then(Value::as_array)
        .and_then(|files| {
            files
                .iter()
                .find(|file| file.get("rel").and_then(Value::as_str) == Some(request.rel.as_str()))
        })
        .and_then(|file| file.get("provenance"))
        .and_then(|provenance| provenance.get("command"))
        .and_then(Value::as_array)
    else {
        return (
            StatusCode::CONFLICT,
            Json(json!({"error":"no declared argv command for this artifact"})),
        )
            .into_response();
    };
    // Même validation que Python : 1-32 arguments, chacun une chaîne de
    // 1 à 2000 caractères, et un seul message d'erreur pour tous les cas.
    let valid = !command.is_empty()
        && command.len() <= 32
        && command.iter().all(|arg| {
            arg.as_str()
                .is_some_and(|text| !text.is_empty() && text.chars().count() <= 2000)
        });
    if !valid {
        return (
            StatusCode::CONFLICT,
            Json(json!({"error":"no declared argv command for this artifact"})),
        )
            .into_response();
    }
    let program = command[0].as_str().unwrap_or_default();
    let args: Vec<&str> = command.iter().skip(1).filter_map(Value::as_str).collect();
    let result = tokio::time::timeout(
        Duration::from_secs(900),
        Command::new(program)
            .args(args)
            .current_dir(&state.root)
            .output(),
    )
    .await;
    match result {
        Ok(Ok(output)) => {
            let ok = output.status.success();
            let mut combined = String::from_utf8_lossy(&output.stdout).into_owned();
            combined.push_str(&String::from_utf8_lossy(&output.stderr));
            if ok {
                // Python relance le rebuild en arrière-plan sans bloquer la réponse.
                let root = state.root.clone();
                let watcher = state.watcher.clone();
                let revision = state.revision.clone();
                let lock = state.rebuild_lock.clone();
                tokio::spawn(async move {
                    rebuild(&root, &watcher, &revision, &lock).await;
                });
            }
            (
                StatusCode::OK,
                Json(json!({
                    "ok": ok,
                    "returncode": output.status.code().unwrap_or(-1),
                    "output": tail_chars(&combined, 6000),
                })),
            )
                .into_response()
        }
        Ok(Err(error)) => (
            StatusCode::BAD_REQUEST,
            Json(json!({"error":error.to_string()})),
        )
            .into_response(),
        Err(_) => (
            StatusCode::REQUEST_TIMEOUT,
            Json(json!({"error":"regeneration timed out"})),
        )
            .into_response(),
    }
}

/// Derniers `limit` caractères (pas octets) d'un texte — équivalent du
/// slicing négatif Python utilisé pour borner les sorties de subprocess.
fn tail_chars(text: &str, limit: usize) -> String {
    let count = text.chars().count();
    text.chars().skip(count.saturating_sub(limit)).collect()
}

/// Même garde que les routes Python à cap explicite : Content-Length requis,
/// non nul et sous la limite (sinon 400 « bad size »).
fn body_size_allowed(headers: &HeaderMap, limit: u64) -> bool {
    headers
        .get(header::CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok())
        .map(|length| length > 0 && length <= limit)
        .unwrap_or(false)
}

#[derive(serde::Deserialize)]
struct IdsRequest {
    ids: Vec<String>,
    destination: Option<String>,
    consumer: Option<String>,
    status: Option<String>,
    result: Option<String>,
    error: Option<String>,
}

#[derive(serde::Deserialize)]
struct RegisterRequest {
    consumer: String,
    destination: String,
    label: Option<String>,
    #[serde(rename = "threadId", alias = "thread_id")]
    thread_id: Option<String>,
    automatic: Option<bool>,
    pid: Option<Value>,
}

#[derive(serde::Deserialize)]
struct SelectionQuery {
    consumer: String,
    destination: Option<String>,
}

fn loopback_origin(headers: &HeaderMap) -> bool {
    let Some(origin) = headers.get("origin").and_then(|value| value.to_str().ok()) else {
        return true;
    };
    let authority = origin
        .strip_prefix("http://")
        .or_else(|| origin.strip_prefix("https://"))
        .and_then(|value| value.split('/').next())
        .unwrap_or_default();
    let host = if let Some(value) = authority.strip_prefix('[') {
        value.split(']').next().unwrap_or_default()
    } else {
        authority.split(':').next().unwrap_or_default()
    };
    matches!(host, "127.0.0.1" | "localhost" | "::1")
}

fn authorized(headers: &HeaderMap, token: &str) -> bool {
    if token.is_empty() {
        return false;
    }
    let expected = format!("Bearer {token}");
    headers
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value == expected)
}

pub fn request_allowed(headers: &HeaderMap, state: &ProjectRuntime) -> bool {
    (!state.remote && loopback_origin(headers)) || authorized(headers, &state.agent_token)
}

fn trusted_static_path(requested: &str, bytes: &[u8]) -> bool {
    if requested.is_empty() || requested == "figures_index.html" {
        return true;
    }
    let Some(rel) = requested.strip_prefix(".fig_thumbs/") else {
        return false;
    };
    let Some(assets) = std::env::var_os("ATELIER_ASSETS_DIR")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("ATELIER_TOOL_ROOT").map(|root| PathBuf::from(root).join("assets"))
        })
    else {
        return false;
    };
    let Ok(candidate) = atelier_core::safe_project_path(&assets, rel) else {
        return false;
    };
    fs::read(candidate).is_ok_and(|bundled| bundled == bytes)
}

const VIDEO_EXTS: &[&str] = &["mp4", "m4v", "mov", "webm"];

fn is_video_path(path: &std::path::Path) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| VIDEO_EXTS.iter().any(|v| v.eq_ignore_ascii_case(e)))
}

/// Préflight CORS global (parité Python `do_OPTIONS` → 200 `{}`).
async fn options_middleware(req: Request, next: Next) -> axum::response::Response {
    if req.method() == Method::OPTIONS {
        return (StatusCode::OK, Json(json!({}))).into_response();
    }
    next.run(req).await
}

async fn remote_auth_middleware(
    State(state): State<ProjectRuntime>,
    req: Request,
    next: Next,
) -> axum::response::Response {
    if state.remote && !authorized(req.headers(), &state.agent_token) {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "bearer token required"})),
        )
            .into_response();
    }
    next.run(req).await
}

async fn static_asset(
    State(state): State<ProjectRuntime>,
    method: Method,
    headers: HeaderMap,
    uri: Uri,
) -> impl IntoResponse {
    if method != Method::GET && method != Method::HEAD {
        return (StatusCode::METHOD_NOT_ALLOWED, "method not allowed").into_response();
    }
    let requested = uri.path().trim_start_matches('/');
    let requested = if requested.is_empty() {
        "figures_index.html"
    } else {
        requested
    };
    let bundled = requested.strip_prefix(".fig_thumbs/").and_then(|rel| {
        std::env::var_os("ATELIER_ASSETS_DIR")
            .map(PathBuf::from)
            .or_else(|| {
                std::env::var_os("ATELIER_TOOL_ROOT").map(|root| PathBuf::from(root).join("assets"))
            })
            .and_then(|assets| atelier_core::safe_project_path(&assets, rel).ok())
            .filter(|path| path.is_file())
    });
    let path = match bundled {
        Some(path) => path,
        None => match atelier_core::safe_project_path(&state.root, requested) {
            Ok(path) => path,
            Err(_) => return (StatusCode::NOT_FOUND, "not found").into_response(),
        },
    };
    let Ok(metadata) = tokio::fs::metadata(&path).await else {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    };
    if !metadata.is_file() {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    }

    // HTTP Range for video (seek in <video>).
    if is_video_path(&path) {
        return serve_video(&path, &metadata, method, &headers).await;
    }

    let Ok(mut bytes) = tokio::fs::read(&path).await else {
        return (StatusCode::INTERNAL_SERVER_ERROR, "read failed").into_response();
    };
    let mut content_type = mime_guess::from_path(&path)
        .first_or_octet_stream()
        .to_string();

    // Inject sel_overlay.js into project HTML (parité Python).
    let is_html = path
        .extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| e.eq_ignore_ascii_case("html") || e.eq_ignore_ascii_case("htm"));
    let trusted = trusted_static_path(requested, &bytes);
    if is_html && !trusted {
        let tag = br#"<script defer src="/.fig_thumbs/sel_overlay.js?v=3"></script>"#;
        let lower = bytes.to_ascii_lowercase();
        if let Some(i) = find_subslice(&lower, b"</body>") {
            let mut out = Vec::with_capacity(bytes.len() + tag.len());
            out.extend_from_slice(&bytes[..i]);
            out.extend_from_slice(tag);
            out.extend_from_slice(&bytes[i..]);
            bytes = out;
        } else {
            bytes.extend_from_slice(tag);
        }
        content_type = "text/html; charset=utf-8".into();
    }

    let mut response = (
        StatusCode::OK,
        [
            (
                header::CONTENT_TYPE,
                HeaderValue::from_str(&content_type)
                    .unwrap_or_else(|_| HeaderValue::from_static("application/octet-stream")),
            ),
            (header::CACHE_CONTROL, HeaderValue::from_static("no-cache")),
        ],
        if method == Method::HEAD {
            Vec::new()
        } else {
            bytes
        },
    )
        .into_response();
    let executable = content_type.starts_with("text/html")
        || content_type == "application/xhtml+xml"
        || content_type == "image/svg+xml";
    if !trusted && executable {
        response.headers_mut().insert(
            header::CONTENT_SECURITY_POLICY,
            HeaderValue::from_static("sandbox allow-scripts allow-forms allow-modals allow-popups"),
        );
    }
    response
}

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).rposition(|w| w == needle)
}

async fn serve_video(
    path: &std::path::Path,
    metadata: &std::fs::Metadata,
    method: Method,
    headers: &HeaderMap,
) -> axum::response::Response {
    let fsize = metadata.len();
    let ctype = mime_guess::from_path(path)
        .first_or_octet_stream()
        .to_string();
    let mut start = 0u64;
    let mut end = fsize.saturating_sub(1);
    let mut partial = false;
    if let Some(rng) = headers.get(header::RANGE).and_then(|v| v.to_str().ok())
        && let Some(spec) = rng.strip_prefix("bytes=")
    {
        let (s, e) = spec.split_once('-').unwrap_or((spec, ""));
        if !s.is_empty() {
            if let Ok(s) = s.parse::<u64>() {
                start = s;
                end = if e.is_empty() {
                    fsize.saturating_sub(1)
                } else {
                    e.parse::<u64>().unwrap_or(fsize.saturating_sub(1))
                };
                partial = true;
            }
        } else if let Ok(suffix) = e.parse::<u64>() {
            start = fsize.saturating_sub(suffix);
            end = fsize.saturating_sub(1);
            partial = true;
        }
        if start > end || start >= fsize {
            return (
                StatusCode::RANGE_NOT_SATISFIABLE,
                [(
                    header::CONTENT_RANGE,
                    HeaderValue::from_str(&format!("bytes */{fsize}"))
                        .unwrap_or_else(|_| HeaderValue::from_static("bytes */0")),
                )],
                Body::empty(),
            )
                .into_response();
        }
        end = end.min(fsize.saturating_sub(1));
    }
    let length = end - start + 1;
    let Ok(mut file) = tokio::fs::File::open(path).await else {
        return (StatusCode::INTERNAL_SERVER_ERROR, "read failed").into_response();
    };
    use tokio::io::{AsyncReadExt, AsyncSeekExt};
    if file.seek(std::io::SeekFrom::Start(start)).await.is_err() {
        return (StatusCode::INTERNAL_SERVER_ERROR, "seek failed").into_response();
    }
    let mut buf = vec![0u8; length as usize];
    if method != Method::HEAD {
        let _ = file.read_exact(&mut buf).await;
    } else {
        buf.clear();
    }
    let status = if partial {
        StatusCode::PARTIAL_CONTENT
    } else {
        StatusCode::OK
    };
    let mut builder = axum::response::Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, ctype)
        .header(header::ACCEPT_RANGES, "bytes")
        .header(
            header::CONTENT_LENGTH,
            if method == Method::HEAD {
                length
            } else {
                buf.len() as u64
            },
        );
    if partial {
        builder = builder.header(
            header::CONTENT_RANGE,
            format!("bytes {start}-{end}/{fsize}"),
        );
    }
    builder
        .body(Body::from(buf))
        .unwrap_or_else(|_| (StatusCode::INTERNAL_SERVER_ERROR, "response").into_response())
}

async fn quote(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
    Json(request): Json<Value>,
) -> impl IntoResponse {
    if !request_allowed(&headers, &state) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"loopback origin required"})),
        )
            .into_response();
    }
    // Mêmes règles que le /quote Python : corps borné à 1 Mo, rel + text
    // obligatoires (chaînes), text tronqué à 100 000, comment à 10 000,
    // message « chemin (p.X) : « … » ».
    if !body_size_allowed(&headers, 1024 * 1024) {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"bad size"}))).into_response();
    }
    let (Some(raw_rel), Some(raw_text)) = (
        request.get("rel").and_then(Value::as_str),
        request.get("text").and_then(Value::as_str),
    ) else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error":"rel and text must be strings"})),
        )
            .into_response();
    };
    let Ok(full) = atelier_core::safe_project_path(&state.root, raw_rel.trim()) else {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error":"file not found"})),
        )
            .into_response();
    };
    if !full.is_file() {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error":"file not found"})),
        )
            .into_response();
    }
    let rel = full
        .strip_prefix(&state.root)
        .unwrap_or(&full)
        .to_string_lossy()
        .replace('\\', "/");
    let text: String = raw_text.trim().chars().take(100_000).collect();
    if text.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error":"text is required"})),
        )
            .into_response();
    }
    let page = request.get("page").cloned().unwrap_or(Value::Null);
    let page_text = match &page {
        Value::String(value) => value.clone(),
        Value::Number(value) => value.to_string(),
        _ => String::new(),
    };
    let loc = if !page_text.is_empty() && page_text != "html" {
        format!(" (p.{page_text})")
    } else {
        String::new()
    };
    let mut message = format!("{}{loc} : \u{ab} {text} \u{bb} ", full.to_string_lossy());
    let comment: String = request
        .get("comment")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .chars()
        .take(10_000)
        .collect();
    if !comment.is_empty() {
        message = format!("{}\nCommentaire : {comment}", message.trim_end());
    }
    let direct = request
        .get("direct")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let held = request
        .get("held")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if state.agent_token.is_empty() {
        return (
            StatusCode::OK,
            Json(json!({
                "ok": true, "message": message, "queuedForAgent": false,
                "agentHost": Value::Null,
            })),
        )
            .into_response();
    }
    let mut agent = state.agent.lock().await;
    let mut payload = serde_json::Map::new();
    payload.insert("type".to_string(), json!("text_annotation"));
    payload.insert("path".to_string(), json!(rel));
    payload.insert("page".to_string(), page);
    payload.insert("selection".to_string(), json!(text));
    payload.insert("comment".to_string(), json!(comment));
    payload.insert(
        "anchor".to_string(),
        crate::agent::normalize_anchor(&request, &rel),
    );
    payload.insert("message".to_string(), json!(message));
    payload.insert("requestedDirect".to_string(), json!(direct));
    payload.extend(agent.delivery(
        request.get("action").and_then(Value::as_str),
        direct,
        request.get("destination").and_then(Value::as_str),
        request.get("batchId").and_then(Value::as_str),
        held,
    ));
    match agent.enqueue_event(payload) {
        Ok(event) => (
            StatusCode::OK,
            Json(json!({
                "embedded": true,
                "message": message,
                "agentHost": "codex",
                "queuedForAgent": true,
                "agentSelectionId": event["id"],
                "agentSelectionStatus": event["status"],
            })),
        )
            .into_response(),
        Err(error) => (StatusCode::CONFLICT, Json(json!({"error": error}))).into_response(),
    }
}

fn safe_annotation_stem(value: &str) -> String {
    let stem = std::path::Path::new(value)
        .file_stem()
        .and_then(|part| part.to_str())
        .unwrap_or("figure");
    let clean: String = stem
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-' | '.') {
                ch
            } else {
                '_'
            }
        })
        .collect();
    if clean.is_empty() {
        "figure".to_string()
    } else {
        clean.chars().take(120).collect()
    }
}

async fn save_annotation(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
    Json(request): Json<Value>,
) -> impl IntoResponse {
    if !request_allowed(&headers, &state) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"loopback origin required"})),
        )
            .into_response();
    }
    let Some(name) = request.get("name").and_then(Value::as_str) else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error":"name and dataURL are required"})),
        )
            .into_response();
    };
    let Some(data_url) = request.get("dataURL").and_then(Value::as_str) else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error":"name and dataURL are required"})),
        )
            .into_response();
    };
    let Some((prefix, encoded)) = data_url.split_once(',') else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error":"PNG dataURL required"})),
        )
            .into_response();
    };
    if !prefix.starts_with("data:image/png;base64") {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error":"PNG dataURL required"})),
        )
            .into_response();
    }
    let Ok(raw) = BASE64.decode(encoded) else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error":"invalid PNG dataURL"})),
        )
            .into_response();
    };
    if raw.is_empty() || raw.len() > 64 * 1024 * 1024 {
        return (
            StatusCode::PAYLOAD_TOO_LARGE,
            Json(json!({"error":"bad image size"})),
        )
            .into_response();
    }
    let out_dir = state.root.join("annotations");
    if let Err(error) = fs::create_dir_all(&out_dir) {
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error":error.to_string()})),
        )
            .into_response();
    }
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let rel = format!(
        "annotations/{}_annot_{}.png",
        safe_annotation_stem(name),
        stamp
    );
    let Ok(path) = atelier_core::safe_project_path(&state.root, &rel) else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error":"invalid annotation path"})),
        )
            .into_response();
    };
    if let Err(error) = tokio::fs::write(&path, raw).await {
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error":error.to_string()})),
        )
            .into_response();
    }
    let direct = request
        .get("direct")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let notes = request.get("notes").cloned().unwrap_or_else(|| json!([]));
    let mut message = rel.clone();
    if let Some(items) = notes.as_array() {
        let lines: Vec<String> = items
            .iter()
            .filter_map(|item| {
                let number = item.get("n").map(Value::to_string)?;
                let text = item.get("text").and_then(Value::as_str)?;
                Some(format!("{}. {}", number.trim_matches('"'), text))
            })
            .collect();
        if !lines.is_empty() {
            message.push_str("\nAnnotations (badges numerotes sur l'image) :\n");
            message.push_str(&lines.join("\n"));
        }
    }
    if direct {
        message.push_str("\nApplique directement ces annotations : retrouve le script qui genere cette figure, fais les corrections demandees et regenere la figure.");
    }
    if state.agent_token.is_empty() {
        return (
            StatusCode::OK,
            Json(json!({
                "embedded": request.get("embed").and_then(Value::as_bool).unwrap_or(false),
                "message": message,
                "agentHost": Value::Null,
                "queuedForAgent": false,
                "path": rel,
            })),
        )
            .into_response();
    }
    let held = request
        .get("held")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let mut agent = state.agent.lock().await;
    let mut payload = serde_json::Map::new();
    payload.insert("type".to_string(), json!("image_annotation"));
    payload.insert("path".to_string(), json!(rel));
    payload.insert(
        "original".to_string(),
        request.get("name").cloned().unwrap_or(Value::Null),
    );
    payload.insert(
        "notes".to_string(),
        crate::agent::normalize_notes(request.get("notes")),
    );
    payload.insert(
        "anchor".to_string(),
        json!({"kind": "image-region", "x": 0, "y": 0, "width": 1, "height": 1}),
    );
    payload.insert("message".to_string(), json!(message));
    payload.insert("requestedDirect".to_string(), json!(direct));
    payload.extend(agent.delivery(
        request.get("action").and_then(Value::as_str),
        direct,
        request.get("destination").and_then(Value::as_str),
        request.get("batchId").and_then(Value::as_str),
        held,
    ));
    let queued = match agent.enqueue_event(payload) {
        Ok(event) => event,
        Err(error) => return (StatusCode::CONFLICT, Json(json!({"error":error}))).into_response(),
    };
    (
        StatusCode::OK,
        Json(json!({
            "embedded": request.get("embed").and_then(Value::as_bool).unwrap_or(false),
            "message": message,
            "agentHost": "codex",
            "queuedForAgent": true,
            "agentSelectionId": queued["id"],
            "agentSelectionStatus": queued["status"],
            "path": rel,
        })),
    )
        .into_response()
}

async fn get_agent_selection(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if !request_allowed(&headers, &state) || !authorized(&headers, &state.agent_token) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"agent authorization required"})),
        )
            .into_response();
    }
    let agent = state.agent.lock().await;
    let (pending, latest) = agent.peek();
    (
        StatusCode::OK,
        Json(json!({
            "ok": true,
            "usage": "POST an annotation here; Codex reads it through the Atelier MCP tool",
            "pending": pending,
            "latest": latest,
        })),
    )
        .into_response()
}

#[derive(serde::Deserialize)]
struct PreferencesRequest {
    destination: String,
    automatic: Option<bool>,
    label: Option<String>,
}

async fn agent_preferences(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
    Json(request): Json<PreferencesRequest>,
) -> impl IntoResponse {
    if !request_allowed(&headers, &state) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"loopback origin required"})),
        )
            .into_response();
    }
    let mut agent = state.agent.lock().await;
    match agent.set_preferences(&request.destination, request.automatic, request.label) {
        Ok(destination) => (
            StatusCode::OK,
            Json(json!({"ok": true, "destination": destination})),
        )
            .into_response(),
        Err(error) => (StatusCode::BAD_REQUEST, Json(json!({"error": error}))).into_response(),
    }
}

#[derive(serde::Deserialize)]
struct BatchRequest {
    #[serde(rename = "batchId", alias = "batch_id")]
    batch_id: Option<String>,
}

async fn batch_release(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
    Json(request): Json<BatchRequest>,
) -> impl IntoResponse {
    if !request_allowed(&headers, &state) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"loopback origin required"})),
        )
            .into_response();
    }
    let batch_id = request.batch_id.unwrap_or_default();
    let mut agent = state.agent.lock().await;
    match agent.release_batch(&batch_id) {
        Ok(released) => {
            let ids: Vec<_> = released
                .iter()
                .filter_map(|item| item.get("id").cloned())
                .collect();
            (
                StatusCode::OK,
                Json(json!({"ok": true, "released": released.len(), "ids": ids})),
            )
                .into_response()
        }
        Err(error) => (StatusCode::BAD_REQUEST, Json(json!({"error": error}))).into_response(),
    }
}

async fn batch_cancel(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
    Json(request): Json<BatchRequest>,
) -> impl IntoResponse {
    if !request_allowed(&headers, &state) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"loopback origin required"})),
        )
            .into_response();
    }
    let batch_id = request.batch_id.unwrap_or_default();
    let mut agent = state.agent.lock().await;
    match agent.cancel_batch(&batch_id) {
        Ok(cancelled) => {
            let ids: Vec<_> = cancelled
                .iter()
                .filter_map(|item| item.get("id").cloned())
                .collect();
            (
                StatusCode::OK,
                Json(json!({"ok": true, "cancelled": cancelled.len(), "ids": ids})),
            )
                .into_response()
        }
        Err(error) => (StatusCode::BAD_REQUEST, Json(json!({"error": error}))).into_response(),
    }
}

async fn selection(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
    Json(request): Json<Value>,
) -> impl IntoResponse {
    if !authorized(&headers, &state.agent_token) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"agent authorization required"})),
        )
            .into_response();
    }
    // Même contrat que le /agent-selection Python : corps borné à 1 Mo,
    // path|rel obligatoire, type par défaut "annotation", aucun champ texte
    // requis (les annotations d'artefact n'en ont pas) — la sélection texte
    // passe par /quote. source/region/anchor/notes suivent les mêmes
    // normalisations.
    if !body_size_allowed(&headers, 1024 * 1024) {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"bad size"}))).into_response();
    }
    let raw_rel = request
        .get("path")
        .or_else(|| request.get("rel"))
        .cloned()
        .unwrap_or(json!(""));
    let Some(requested_path) = raw_rel.as_str().map(str::trim) else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error":"path must be a string"})),
        )
            .into_response();
    };
    let mut agent = state.agent.lock().await;
    let rel = match agent.resolve_rel(requested_path) {
        Ok(rel) if !requested_path.is_empty() => rel,
        _ => {
            return (
                StatusCode::NOT_FOUND,
                Json(json!({"error": format!("file not found: {requested_path}")})),
            )
                .into_response();
        }
    };
    let source = request
        .get("source")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .and_then(|value| agent.resolve_rel(value).ok());
    let event_type: String = request
        .get("type")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or("annotation")
        .chars()
        .take(80)
        .collect();
    let comment: String = request
        .get("comment")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .chars()
        .take(10_000)
        .collect();
    let direct = request
        .get("direct")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let held = request
        .get("held")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let region = request
        .get("region")
        .filter(|value| value.is_object())
        .cloned()
        .unwrap_or(Value::Null);
    let mut payload = serde_json::Map::new();
    payload.insert("type".to_string(), json!(event_type));
    payload.insert("path".to_string(), json!(rel));
    payload.insert(
        "source".to_string(),
        source.map(|s| json!(s)).unwrap_or(Value::Null),
    );
    payload.insert("comment".to_string(), json!(comment));
    payload.insert("region".to_string(), region);
    payload.insert(
        "anchor".to_string(),
        crate::agent::normalize_anchor(&request, &rel),
    );
    payload.insert(
        "notes".to_string(),
        crate::agent::normalize_notes(request.get("notes")),
    );
    payload.insert("requestedDirect".to_string(), json!(direct));
    payload.extend(agent.delivery(
        request.get("action").and_then(Value::as_str),
        direct,
        request.get("destination").and_then(Value::as_str),
        request.get("batchId").and_then(Value::as_str),
        held,
    ));
    match agent.enqueue_event(payload) {
        Ok(event) => (
            StatusCode::OK,
            Json(json!({"ok":true,"queuedForAgent":true,"id":event["id"]})),
        )
            .into_response(),
        Err(error) => (StatusCode::BAD_REQUEST, Json(json!({"error":error}))).into_response(),
    }
}

async fn register_consumer(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
    Json(request): Json<RegisterRequest>,
) -> impl IntoResponse {
    if !authorized(&headers, &state.agent_token) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"agent authorization required"})),
        )
            .into_response();
    }
    let mut agent = state.agent.lock().await;
    match agent.register(
        request.destination,
        request.consumer,
        request.label,
        request.thread_id,
        request.automatic,
        request.pid,
    ) {
        Ok(destination) => (
            StatusCode::OK,
            Json(json!({"ok":true,"destination":destination})),
        )
            .into_response(),
        Err(error) => (StatusCode::BAD_REQUEST, Json(json!({"error":error}))).into_response(),
    }
}

async fn selections(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
    Query(query): Query<SelectionQuery>,
) -> impl IntoResponse {
    if !authorized(&headers, &state.agent_token) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"agent authorization required"})),
        )
            .into_response();
    }
    let destination = query.destination.unwrap_or_else(|| query.consumer.clone());
    let mut agent = state.agent.lock().await;
    match agent.claim(&query.consumer, &destination) {
        Ok(items) => {
            let count = items.len();
            (StatusCode::OK, Json(json!({"items":items,"count":count}))).into_response()
        }
        Err(error) => (StatusCode::BAD_REQUEST, Json(json!({"error":error}))).into_response(),
    }
}

async fn acknowledge(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
    Json(request): Json<IdsRequest>,
) -> impl IntoResponse {
    if !authorized(&headers, &state.agent_token) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"agent authorization required"})),
        )
            .into_response();
    }
    let mut agent = state.agent.lock().await;
    match agent.acknowledge(
        &request.ids,
        request.consumer.as_deref().unwrap_or_default(),
    ) {
        Ok(count) => (
            StatusCode::OK,
            Json(json!({"ok":true,"acknowledged":count})),
        )
            .into_response(),
        Err(error) => (StatusCode::BAD_REQUEST, Json(json!({"error":error}))).into_response(),
    }
}

async fn release(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
    Json(request): Json<IdsRequest>,
) -> impl IntoResponse {
    if !request_allowed(&headers, &state) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"loopback origin required"})),
        )
            .into_response();
    }
    let Some(destination) = request.destination else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error":"destination is required"})),
        )
            .into_response();
    };
    let mut agent = state.agent.lock().await;
    match agent.release(&request.ids, destination) {
        Ok(count) => (StatusCode::OK, Json(json!({"ok":true,"released":count}))).into_response(),
        Err(error) => (StatusCode::BAD_REQUEST, Json(json!({"error":error}))).into_response(),
    }
}

async fn delete_annotations(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
    Json(request): Json<IdsRequest>,
) -> impl IntoResponse {
    if !request_allowed(&headers, &state) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"loopback origin required"})),
        )
            .into_response();
    }
    let mut agent = state.agent.lock().await;
    match agent.delete(&request.ids) {
        Ok(count) => (StatusCode::OK, Json(json!({"ok":true,"deleted":count}))).into_response(),
        Err(error) => (StatusCode::BAD_REQUEST, Json(json!({"error":error}))).into_response(),
    }
}

async fn restore_annotations(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
    Json(request): Json<IdsRequest>,
) -> impl IntoResponse {
    if !request_allowed(&headers, &state) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"loopback origin required"})),
        )
            .into_response();
    }
    let mut agent = state.agent.lock().await;
    match agent.restore(&request.ids) {
        Ok(count) => (StatusCode::OK, Json(json!({"ok":true,"restored":count}))).into_response(),
        Err(error) => (StatusCode::BAD_REQUEST, Json(json!({"error":error}))).into_response(),
    }
}

async fn annotation_status(
    State(state): State<ProjectRuntime>,
    headers: HeaderMap,
    Json(request): Json<IdsRequest>,
) -> impl IntoResponse {
    if !authorized(&headers, &state.agent_token) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"agent authorization required"})),
        )
            .into_response();
    }
    let mut agent = state.agent.lock().await;
    match agent.update_status(
        &request.ids,
        request.status.as_deref().unwrap_or_default(),
        request.result.as_deref().unwrap_or_default(),
        request.error.as_deref().unwrap_or_default(),
    ) {
        Ok(count) => (StatusCode::OK, Json(json!({"ok":true,"updated":count}))).into_response(),
        Err(error) => (StatusCode::BAD_REQUEST, Json(json!({"error":error}))).into_response(),
    }
}

async fn rescan(State(state): State<ProjectRuntime>, headers: HeaderMap) -> impl IntoResponse {
    if !request_allowed(&headers, &state) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error":"loopback origin required"})),
        )
            .into_response();
    }
    let outcome = rebuild(
        &state.root,
        &state.watcher,
        &state.revision,
        &state.rebuild_lock,
    )
    .await;
    (
        StatusCode::OK,
        Json(json!({"ok": outcome.ok, "out": outcome.out})),
    )
        .into_response()
}

/// Résultat d'un rebuild — ce que /rescan renvoie au client, aligné sur le
/// contrat Python `{"ok": rc == 0, "out": derniers 200 caractères}`.
pub struct RebuildOutcome {
    pub ok: bool,
    pub out: String,
}

impl RebuildOutcome {
    fn failure(message: impl Into<String>) -> Self {
        Self {
            ok: false,
            out: message.into(),
        }
    }
}

pub async fn rebuild(
    root: &std::path::Path,
    status: &Arc<RwLock<WatcherStatus>>,
    revision: &Arc<RwLock<u64>>,
    rebuild_lock: &Arc<Mutex<()>>,
) -> RebuildOutcome {
    let _guard = rebuild_lock.lock().await;
    let assets = std::env::var_os("ATELIER_ASSETS_DIR")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("ATELIER_TOOL_ROOT").map(|path| PathBuf::from(path).join("assets"))
        });
    let Some(assets) = assets.filter(|path| path.join("gallery_template.html").is_file()) else {
        let message = "Atelier assets directory is not configured";
        status.write().await.error = Some(message.to_string());
        return RebuildOutcome::failure(message);
    };
    let options = atelier_core::gallery_builder::GalleryBuildOptions {
        root: root.to_path_buf(),
        template: assets.join("gallery_template.html"),
        title: std::env::var("GALLERY_TITLE").unwrap_or_else(|_| "Atelier".into()),
        extensions: atelier_core::gallery_builder::parse_extensions(
            std::env::var("GALLERY_EXTS").ok().as_deref(),
        ),
        show_frames: std::env::var_os("GALLERY_SHOW_FRAMES").is_some(),
        no_thumbs: std::env::var_os("GALLERY_NO_THUMBS").is_some(),
    };
    let result =
        tokio::task::spawn_blocking(move || atelier_core::gallery_builder::build(&options)).await;
    let mut current = status.write().await;
    match result {
        Ok(Ok(built)) => {
            *revision.write().await += 1;
            current.last_build_at = Some(now());
            current.error = None;
            RebuildOutcome {
                ok: true,
                out: format!("{} files indexed -> {}", built.count, built.index.display()),
            }
        }
        Ok(Err(error)) => {
            let message = error.to_string();
            current.error = Some(message.clone());
            RebuildOutcome::failure(message)
        }
        Err(error) => RebuildOutcome::failure(error.to_string()),
    }
}

async fn start_watcher(state: ProjectRuntime) -> Result<(), String> {
    let (tx, mut rx) = mpsc::unbounded_channel::<Result<Event, notify::Error>>();
    let root = state.root.clone();
    let tx_for_watcher = tx.clone();
    let mut watcher = notify::recommended_watcher(move |event| {
        let _ = tx_for_watcher.send(event);
    })
    .map_err(|error| error.to_string())?;
    watcher
        .watch(&root, RecursiveMode::Recursive)
        .map_err(|error| error.to_string())?;
    {
        let mut status = state.watcher.write().await;
        status.running = true;
    }
    let _keep_watcher_alive = watcher;
    let mut pending: Vec<String> = Vec::new();
    loop {
        tokio::select! {
            Some(event) = rx.recv() => {
                match event {
                    Ok(event) if matches!(event.kind, EventKind::Create(_) | EventKind::Modify(_) | EventKind::Remove(_)) => {
                        let changed: Vec<String> = event.paths.iter().filter_map(|path| relevant_change(&root, path)).collect();
                        pending.extend(changed);
                        pending.sort();
                        pending.dedup();
                        let mut status = state.watcher.write().await;
                        status.last_event_at = Some(now());
                        status.last_changed = pending.iter().take(50).cloned().collect();
                    }
                    Err(error) => state.watcher.write().await.error = Some(error.to_string()),
                    _ => {}
                }
            }
            _ = sleep(Duration::from_millis(900)), if !pending.is_empty() => {
                let changed = std::mem::take(&mut pending);
                rebuild(
                    &root,
                    &state.watcher,
                    &state.revision,
                    &state.rebuild_lock,
                )
                .await;
                state.watcher.write().await.last_changed = changed.into_iter().take(50).collect();
            }
        }
    }
}

/// Configuration for the legacy mono-project binary.
#[derive(Debug, Clone)]
pub struct LegacyServerConfig {
    pub root: PathBuf,
    pub port: u16,
    pub host: String,
    pub watch: bool,
}

/// Build the legacy un-prefixed project router (exact historical routes).
pub fn legacy_project_router(state: ProjectRuntime) -> Router {
    Router::new()
        .route("/ping", get(ping))
        .route("/health", get(health))
        .route("/rev", get(revision))
        .route("/data", get(data))
        .route("/state", get(gallery_state).post(save_gallery_state))
        // Phase 1 — fichiers, état, éditeurs
        .route("/ls", get(crate::files::ls))
        .route("/snippet", get(crate::files::snippet))
        .route("/raw", get(crate::files::raw))
        .route("/code", get(crate::files::code))
        .route("/texroot", get(crate::files::texroot))
        .route("/findscript", get(crate::files::findscript))
        .route("/codesave", post(crate::files::codesave))
        .route("/save-svg", post(crate::files::save_svg))
        .route("/selinfo", post(crate::files::selinfo))
        // Phase 2 — galerie, miniatures, actions, toast events
        .route("/thumb", get(crate::gallery::thumb))
        .route("/rasterize", get(crate::gallery::rasterize))
        .route("/delete", post(crate::gallery::delete))
        .route("/export", post(crate::gallery::export))
        .route("/open", post(crate::gallery::open_path))
        .route("/clear-quote", post(crate::gallery::clear_quote))
        .route("/claude-targets", get(crate::gallery::claude_targets))
        .route("/quote", get(crate::gallery::get_quote).post(quote))
        .route("/agent-events", get(crate::gallery::agent_events))
        .route("/claude-events", get(crate::gallery::agent_events))
        .route("/agent-event", post(crate::gallery::post_agent_event))
        .route("/claude-event", post(crate::gallery::post_agent_event))
        // Phase 3 — Git + historique de versions
        .route("/githead", get(crate::git::githead))
        .route("/gitlog", get(crate::git::gitlog))
        .route("/gitshow", get(crate::git::gitshow))
        .route("/commitmsg", post(crate::git::commitmsg))
        .route("/gitcommit", post(crate::git::gitcommit))
        .route(
            "/versions",
            get(crate::git::get_versions).post(crate::git::post_versions),
        )
        // Phase 4 — LaTeX / PDF / export PNG
        .route("/compile", post(crate::documents::compile))
        .route("/synctex", post(crate::documents::synctex))
        .route(
            "/pdfannot",
            get(crate::documents::get_pdfannot).post(crate::documents::post_pdfannot),
        )
        .route("/export-png", post(crate::documents::export_png))
        .route("/lint", get(crate::documents::lint))
        // Phase 5 — notes + whiteboard
        .route("/notes/load", get(crate::workspace::notes_load))
        .route("/notes/save", post(crate::workspace::notes_save))
        .route("/board/load", get(crate::workspace::board_load))
        .route("/board/save", post(crate::workspace::board_save))
        .route("/board/poll", get(crate::workspace::board_poll))
        .route("/board/command", post(crate::workspace::board_command))
        .route("/notes/open-surface", post(crate::workspace::open_surface))
        .route("/board/open-surface", post(crate::workspace::open_surface))
        // Phase 6 — Zotero
        .route("/zotero-items", get(crate::zotero::zotero_items))
        .route(
            "/zotero-collections",
            get(crate::zotero::zotero_collections),
        )
        .route("/zotero-fav", post(crate::zotero::zotero_fav))
        .route("/zotero-add", post(crate::zotero::zotero_add))
        .route("/zotero/{key}/{fname}", get(crate::zotero::zotero_pdf))
        // Phase 7 — hôte macOS
        .route(
            "/orca-fullscreen-exit",
            post(crate::host::orca_fullscreen_exit),
        )
        .route(
            "/orca-native-fullscreen",
            post(crate::host::orca_native_fullscreen),
        )
        // Phase 8 — agent bridge remaining endpoints
        .route("/agent-status", get(agent_status))
        .route("/provenance", get(provenance))
        .route("/regenerate", post(regenerate))
        .route("/rescan", post(rescan))
        .route("/save", post(save_annotation))
        .route("/agent-selection", get(get_agent_selection).post(selection))
        .route("/agent-consumers/register", post(register_consumer))
        .route("/agent-selections", get(selections))
        .route("/agent-selections/ack", post(acknowledge))
        .route("/agent-annotations/release", post(release))
        .route("/agent-annotations/delete", post(delete_annotations))
        .route("/agent-annotations/restore", post(restore_annotations))
        .route("/agent-annotations/status", post(annotation_status))
        .route("/agent-preferences", post(agent_preferences))
        .route("/agent-batches/release", post(batch_release))
        .route("/agent-batches/cancel", post(batch_cancel))
        .fallback(static_asset)
        .layer(middleware::from_fn(options_middleware))
        .layer(middleware::from_fn_with_state(
            state.clone(),
            remote_auth_middleware,
        ))
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

/// Run the historical mono-project HTTP server.
pub async fn run_legacy_server(
    config: LegacyServerConfig,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let root = std::fs::canonicalize(&config.root)?;
    let remote = !matches!(
        config.host.as_str(),
        "127.0.0.1" | "localhost" | "::1" | "[::1]"
    );
    if remote && std::env::var("ATELIER_ALLOW_REMOTE").as_deref() != Ok("1") {
        return Err("refusing non-loopback bind; set ATELIER_ALLOW_REMOTE=1 explicitly".into());
    }
    let agent_token = std::env::var("ATELIER_AGENT_TOKEN").unwrap_or_default();
    if remote && agent_token.is_empty() {
        return Err("remote bind requires ATELIER_AGENT_TOKEN".into());
    }
    let state = ProjectRuntime::new_legacy(root, config.port, agent_token, remote, config.watch);
    if config.watch {
        let watcher_state = state.clone();
        tokio::spawn(async move {
            if let Err(error) = start_watcher(watcher_state.clone()).await {
                let mut status = watcher_state.watcher.write().await;
                status.error = Some(error);
                status.running = false;
            }
        });
    }
    let app = legacy_project_router(state);
    let address = format!("{}:{}", config.host, config.port);
    let listener = tokio::net::TcpListener::bind(&address).await?;
    eprintln!("atelier Rust backend listening on http://{address}");
    axum::serve(listener, app).await?;
    Ok(())
}
