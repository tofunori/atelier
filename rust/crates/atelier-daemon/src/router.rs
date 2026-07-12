//! Public HTTP routes for the daemon.

use crate::{
    build_info::{BuildInfo, RuntimeClock},
    config::DaemonConfig,
    host::{HostError, ProjectHost},
    registry::ProjectRegistry,
    sessions::SessionStore,
};
use atelier_server::legacy_project_router;
use axum::{
    Json, Router,
    body::Body,
    extract::{Path, Request, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode, Uri, header},
    middleware::{self, Next},
    response::{IntoResponse, Redirect, Response},
    routing::{any, get},
};
use serde_json::{Value, json};
use std::sync::Arc;
use tower::ServiceExt;
use tower_http::trace::TraceLayer;

#[derive(Clone)]
pub struct DaemonHttpState {
    pub config: Arc<DaemonConfig>,
    pub build: Arc<BuildInfo>,
    pub clock: Arc<RuntimeClock>,
    pub registry: ProjectRegistry,
    pub host: ProjectHost,
    pub sessions: SessionStore,
}

pub fn public_router(state: DaemonHttpState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/version", get(version))
        .route("/open/{ticket}", get(open_ticket))
        .route("/assets/{*path}", get(shared_asset))
        // Legacy absolute UI paths (gallery_template still emits /.fig_thumbs/…).
        // Prefer project-scoped URLs after HTML rewrite; this route is the safety net
        // and serves the same shared assets directory.
        .route("/.fig_thumbs/{*path}", get(fig_thumbs_asset))
        .route("/p/{project_key}/api/v1/events", get(project_events_sse))
        .route("/p/{project_key}/events", get(project_events_sse))
        .route("/p/{project_key}", any(project_dispatch))
        .route("/p/{project_key}/{*rest}", any(project_dispatch))
        .layer(middleware::from_fn(security_headers))
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

async fn healthz(State(state): State<DaemonHttpState>) -> impl IntoResponse {
    (
        StatusCode::OK,
        Json(json!({
            "ok": true,
            "service": "atelier-daemon",
            "pid": std::process::id(),
            "uptimeSecs": state.clock.uptime_secs(),
            "port": state.config.port,
        })),
    )
}

async fn version(State(state): State<DaemonHttpState>) -> Json<Value> {
    Json(json!({
        "ok": true,
        "version": state.build.version,
        "gitSha": state.build.git_sha,
        "buildTimestamp": state.build.build_timestamp,
        "target": state.build.target,
        "protocolVersion": state.build.protocol_version,
        "assetHash": state.build.asset_hash,
        "daemonInstance": state.build.daemon_instance,
        "pid": std::process::id(),
        "uptimeSecs": state.clock.uptime_secs(),
        "projects": state.registry.project_count().await,
    }))
}

async fn open_ticket(State(state): State<DaemonHttpState>, Path(ticket): Path<String>) -> Response {
    match state.sessions.consume_ticket(&ticket).await {
        Ok((session, project_key, theme, native_fs)) => {
            let mut location = format!(
                "/p/{project_key}/figures_index.html?nativeFs={}",
                if native_fs { "1" } else { "0" }
            );
            if let Some(theme) = theme {
                location.push_str(&format!("&theme={}", urlencoding_minimal(&theme)));
            } else {
                location.push_str("&theme=Codex");
            }
            let cookie = format!(
                "atelier_session={session}; Path=/p/{project_key}/; HttpOnly; SameSite=Strict; Max-Age=2592000"
            );
            let mut response = Redirect::temporary(&location).into_response();
            if let Ok(value) = HeaderValue::from_str(&cookie) {
                response.headers_mut().append(header::SET_COOKIE, value);
            }
            response
        }
        Err(message) => (
            StatusCode::UNAUTHORIZED,
            Json(json!({
                "code": "SESSION_INVALID",
                "message": message,
            })),
        )
            .into_response(),
    }
}

async fn shared_asset(State(state): State<DaemonHttpState>, Path(path): Path<String>) -> Response {
    serve_from_assets_dir(&state, &path)
}

async fn fig_thumbs_asset(
    State(state): State<DaemonHttpState>,
    Path(path): Path<String>,
) -> Response {
    // Same shared tree as /assets/* — historical mono-server URL shape.
    serve_from_assets_dir(&state, &path)
}

fn serve_from_assets_dir(state: &DaemonHttpState, path: &str) -> Response {
    let root = state
        .config
        .assets_dir
        .clone()
        .or_else(|| std::env::var_os("ATELIER_ASSETS_DIR").map(std::path::PathBuf::from))
        .or_else(|| {
            std::env::current_exe()
                .ok()
                .and_then(|exe| exe.parent().map(|p| p.join("../share/atelier/assets")))
        });
    let Some(root) = root.filter(|p| p.is_dir()) else {
        return (StatusCode::NOT_FOUND, "assets not configured").into_response();
    };
    read_asset(&root, path)
}

fn read_asset(root: &std::path::Path, rel: &str) -> Response {
    let Ok(path) = atelier_core::safe_project_path(root, rel) else {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    };
    match std::fs::read(&path) {
        Ok(bytes) => {
            let mime = mime_guess::from_path(&path)
                .first_or_octet_stream()
                .to_string();
            (
                StatusCode::OK,
                [
                    (header::CONTENT_TYPE, mime),
                    (
                        header::CACHE_CONTROL,
                        "public, max-age=31536000, immutable".into(),
                    ),
                ],
                bytes,
            )
                .into_response()
        }
        Err(_) => (StatusCode::NOT_FOUND, "not found").into_response(),
    }
}

async fn project_dispatch(State(state): State<DaemonHttpState>, req: Request) -> Response {
    let path = req.uri().path().to_string();
    let Some((project_key, stripped)) = split_project_path(&path) else {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({
                "code": "PROJECT_NOT_FOUND",
                "message": "invalid project path",
            })),
        )
            .into_response();
    };

    // Session gate (Phase 4). Allow health-like probes without cookie? Plan: require session for project pages/API.
    let cookie = cookie_value(req.headers(), "atelier_session");
    let authorized = match cookie.as_deref() {
        Some(raw) => state.sessions.validate_session(raw, &project_key).await,
        None => false,
    };
    // Temporary development escape hatch for automated tests / local debugging.
    let bypass = std::env::var("ATELIER_DAEMON_ALLOW_ANON")
        .map(|v| v == "1")
        .unwrap_or(false);
    if !authorized && !bypass {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({
                "code": "SESSION_INVALID",
                "message": "reopen Atelier from the plugin to establish a session",
            })),
        )
            .into_response();
    }

    // Daemon-owned project switcher API. It lives behind the current
    // project session gate; opening another project returns a one-shot ticket
    // so the browser receives a cookie scoped to the destination project.
    if stripped == "/api/projects" && req.method() == Method::GET {
        return (
            StatusCode::OK,
            Json(json!({
                "ok": true,
                "currentProject": project_key,
                "projects": state.host.list_public().await,
            })),
        )
            .into_response();
    }
    if stripped == "/api/projects/open" && req.method() == Method::POST {
        return open_project_from_browser(&state, req).await;
    }

    let runtime = match state.host.activate(&project_key).await {
        Ok(runtime) => runtime,
        Err(error) => return host_error(error),
    };

    let mut builder = Request::builder()
        .method(req.method().clone())
        .uri(rewrite_uri(req.uri(), &stripped))
        .version(req.version());
    *builder.headers_mut().unwrap() = req.headers().clone();
    let forwarded = match builder.body(req.into_body()) {
        Ok(request) => request,
        Err(error) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": error.to_string()})),
            )
                .into_response();
        }
    };

    match legacy_project_router(runtime).oneshot(forwarded).await {
        Ok(response) => inject_bootstrap_if_html(response, &project_key, &state.build).await,
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": error.to_string()})),
        )
            .into_response(),
    }
}

async fn open_project_from_browser(state: &DaemonHttpState, req: Request) -> Response {
    let bytes = match axum::body::to_bytes(req.into_body(), 64 * 1024).await {
        Ok(bytes) => bytes,
        Err(error) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"ok": false, "message": error.to_string()})),
            )
                .into_response();
        }
    };
    let payload: Value = match serde_json::from_slice(&bytes) {
        Ok(value) => value,
        Err(error) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"ok": false, "message": format!("invalid JSON: {error}")})),
            )
                .into_response();
        }
    };

    let key = if let Some(key) = payload.get("key").and_then(Value::as_str) {
        if state.registry.get(key).await.is_none() {
            return (
                StatusCode::NOT_FOUND,
                Json(json!({"ok": false, "message": "project not found"})),
            )
                .into_response();
        }
        key.to_string()
    } else if let Some(root) = payload.get("root").and_then(Value::as_str) {
        let expanded = expand_user_path(root);
        match state.registry.register(&expanded).await {
            Ok((key, _)) => key,
            Err(message) => {
                return (
                    StatusCode::BAD_REQUEST,
                    Json(json!({"ok": false, "message": format!("cannot open folder: {message}")})),
                )
                    .into_response();
            }
        }
    } else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"ok": false, "message": "key or root required"})),
        )
            .into_response();
    };

    if let Err(error) = state.host.activate(&key).await {
        return host_error(error);
    }
    let theme = payload
        .get("theme")
        .and_then(Value::as_str)
        .unwrap_or("Codex");
    let native_fs = payload
        .get("nativeFs")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    match state
        .sessions
        .mint_ticket(&key, None, Some(theme.to_string()), native_fs)
        .await
    {
        Ok(ticket) => (
            StatusCode::OK,
            Json(json!({
                "ok": true,
                "key": key,
                "openUrl": format!("/open/{ticket}"),
            })),
        )
            .into_response(),
        Err(message) => (
            StatusCode::TOO_MANY_REQUESTS,
            Json(json!({"ok": false, "message": message})),
        )
            .into_response(),
    }
}

fn expand_user_path(raw: &str) -> std::path::PathBuf {
    let trimmed = raw.trim();
    if trimmed == "~" {
        return std::env::var_os("HOME")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| std::path::PathBuf::from(trimmed));
    }
    if let Some(rest) = trimmed.strip_prefix("~/")
        && let Some(home) = std::env::var_os("HOME")
    {
        return std::path::PathBuf::from(home).join(rest);
    }
    std::path::PathBuf::from(trimmed)
}

/// Rebuild HTML responses with the non-executable bootstrap JSON and runtime scripts.
/// Never drops the original body: on failure the original bytes are restored.
async fn inject_bootstrap_if_html(
    response: Response,
    project_key: &str,
    build: &BuildInfo,
) -> Response {
    let (mut parts, body) = response.into_parts();
    let content_type = parts
        .headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .to_string();

    let bytes = match axum::body::to_bytes(body, 32 * 1024 * 1024).await {
        Ok(bytes) => bytes,
        Err(error) => {
            // Never return an empty body: the original stream is consumed.
            let msg = format!(
                "<!doctype html><html><head><meta charset=\"utf-8\"><title>Atelier</title></head>\
<body><p>Atelier could not buffer this HTML for bootstrap injection: {error}</p>\
<p>Reopen Atelier from the plugin.</p></body></html>"
            );
            parts.headers.insert(
                header::CONTENT_TYPE,
                HeaderValue::from_static("text/html; charset=utf-8"),
            );
            parts.headers.remove(header::CONTENT_LENGTH);
            return Response::from_parts(parts, Body::from(msg));
        }
    };

    let looks_like_html = content_type.contains("text/html") || {
        let head = String::from_utf8_lossy(&bytes[..bytes.len().min(256)]).to_ascii_lowercase();
        head.contains("<!doctype html") || head.contains("<html")
    };

    if !looks_like_html {
        return Response::from_parts(parts, Body::from(bytes));
    }

    let original = String::from_utf8_lossy(&bytes);
    // Versioned asset directories are not implemented yet — always use /assets.
    let asset_base = "/assets".to_string();
    let base_path = format!("/p/{project_key}");
    // Project routes are still the legacy paths mounted under /p/{key}/… (not /api/v1 yet).
    let bootstrap = json!({
        "projectKey": project_key,
        "basePath": base_path,
        "apiBase": base_path,
        "assetBase": asset_base,
        "daemonInstance": build.daemon_instance,
    });
    let injection = format!(
        r#"<script type="application/json" id="atelier-bootstrap">{bootstrap}</script>
<script src="{asset_base}/atelier_runtime.js"></script>
<script src="{asset_base}/atelier_events.js"></script>
"#
    );
    // Rewrite bare absolute /.fig_thumbs/ (legacy mono-server paths) into the
    // project-scoped prefix so session cookies and static_asset apply.
    let rewritten = rewrite_fig_thumbs_for_project(&original, project_key);
    let injected = inject_before_head_close(&rewritten, &injection);

    if let Ok(value) = HeaderValue::from_str(project_key) {
        parts.headers.insert(
            header::HeaderName::from_static("x-atelier-project-key"),
            value,
        );
    }
    if let Ok(value) = HeaderValue::from_str(&build.daemon_instance) {
        parts.headers.insert(
            header::HeaderName::from_static("x-atelier-daemon-instance"),
            value,
        );
    }
    if !content_type.contains("text/html") {
        parts.headers.insert(
            header::CONTENT_TYPE,
            HeaderValue::from_static("text/html; charset=utf-8"),
        );
    }
    parts.headers.remove(header::CONTENT_LENGTH);

    Response::from_parts(parts, Body::from(injected))
}

/// Rewrite absolute `/.fig_thumbs/...` (and unprefixed variants) to
/// `/p/{project_key}/.fig_thumbs/...` so the project router serves them.
fn rewrite_fig_thumbs_for_project(html: &str, project_key: &str) -> String {
    let scoped = format!("/p/{project_key}/.fig_thumbs/");
    // Avoid double-prefixing already-scoped URLs.
    let mut out = html.replace(&scoped, "\u{0001}SCOPED_FIG_THUMBS\u{0001}");
    out = out.replace("/.fig_thumbs/", &scoped);
    out = out.replace("\u{0001}SCOPED_FIG_THUMBS\u{0001}", &scoped);
    out
}

fn inject_before_head_close(html: &str, injection: &str) -> String {
    // Prefer </head> (case-insensitive search on a lowered copy, splice original).
    let lower = html.to_ascii_lowercase();
    if let Some(idx) = lower.find("</head>") {
        let mut out = String::with_capacity(html.len() + injection.len());
        out.push_str(&html[..idx]);
        out.push_str(injection);
        out.push_str(&html[idx..]);
        return out;
    }
    if let Some(idx) = lower.find("<body")
        && let Some(end) = lower[idx..].find('>')
    {
        let insert_at = idx + end + 1;
        let mut out = String::with_capacity(html.len() + injection.len());
        out.push_str(&html[..insert_at]);
        out.push_str(injection);
        out.push_str(&html[insert_at..]);
        return out;
    }
    // No head/body markers: prepend so the page content is never lost.
    let mut out = String::with_capacity(html.len() + injection.len());
    out.push_str(injection);
    out.push_str(html);
    out
}

fn cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    let cookie = headers.get(header::COOKIE)?.to_str().ok()?;
    for part in cookie.split(';') {
        let part = part.trim();
        if let Some(value) = part.strip_prefix(&format!("{name}=")) {
            return Some(value.to_string());
        }
    }
    None
}

async fn project_events_sse(State(state): State<DaemonHttpState>, req: Request) -> Response {
    use axum::response::sse::{Event, KeepAlive, Sse};
    use futures_util::stream::{self, StreamExt};
    use std::convert::Infallible;
    use tokio_stream::wrappers::BroadcastStream;

    let path = req.uri().path().to_string();
    let Some((project_key, _)) = split_project_path(&path) else {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    };
    let cookie = cookie_value(req.headers(), "atelier_session");
    let authorized = match cookie.as_deref() {
        Some(raw) => state.sessions.validate_session(raw, &project_key).await,
        None => std::env::var("ATELIER_DAEMON_ALLOW_ANON")
            .map(|v| v == "1")
            .unwrap_or(false),
    };
    if !authorized {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"code":"SESSION_INVALID","message":"session required"})),
        )
            .into_response();
    }
    // Activate so the event bus exists, then subscribe.
    if let Err(error) = state.host.activate(&project_key).await {
        return host_error(error);
    }
    let Some(bus) = state.host.event_bus(&project_key).await else {
        return (StatusCode::SERVICE_UNAVAILABLE, "no event bus").into_response();
    };

    let last_event_id = req
        .headers()
        .get("last-event-id")
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.parse::<u64>().ok())
        .unwrap_or(0);

    let revision = state
        .host
        .activate(&project_key)
        .await
        .map(|r| r.revision())
        .ok();
    let rev = if let Some(r) = revision {
        *r.read().await
    } else {
        0
    };

    let ready = bus.ready_event(rev);
    let mut backlog = bus.since(last_event_id);
    if last_event_id == 0 {
        backlog.insert(0, ready);
    }
    let rx = bus.subscribe();

    let initial = stream::iter(
        backlog
            .into_iter()
            .map(|event| {
                Ok::<_, Infallible>(
                    Event::default()
                        .id(event.seq.to_string())
                        .event("message")
                        .data(serde_json::to_string(&event).unwrap_or_else(|_| "{}".into())),
                )
            })
            .collect::<Vec<_>>(),
    );

    let live = BroadcastStream::new(rx).filter_map(|item| async move {
        match item {
            Ok(event) => Some(Ok::<_, Infallible>(
                Event::default()
                    .id(event.seq.to_string())
                    .event("message")
                    .data(serde_json::to_string(&event).unwrap_or_else(|_| "{}".into())),
            )),
            Err(_) => None, // lag — client should reconnect with Last-Event-ID
        }
    });

    let stream = initial.chain(live);
    Sse::new(stream)
        .keep_alive(KeepAlive::default())
        .into_response()
}

fn split_project_path(path: &str) -> Option<(String, String)> {
    let rest = path.strip_prefix("/p/")?;
    let mut parts = rest.splitn(2, '/');
    let key = parts.next()?.to_string();
    if key.is_empty() || key.len() != 24 || !key.chars().all(|c| c.is_ascii_hexdigit()) {
        return None;
    }
    let remainder = parts.next().unwrap_or("");
    let stripped = if remainder.is_empty() {
        "/".to_string()
    } else {
        format!("/{remainder}")
    };
    Some((key, stripped))
}

fn rewrite_uri(original: &Uri, new_path: &str) -> Uri {
    let mut parts = original.clone().into_parts();
    let path_and_query = match original.query() {
        Some(query) => format!("{new_path}?{query}"),
        None => new_path.to_string(),
    };
    parts.path_and_query = path_and_query.parse().ok();
    Uri::from_parts(parts).unwrap_or_else(|_| Uri::from_static("/"))
}

fn host_error(error: HostError) -> Response {
    let status =
        StatusCode::from_u16(error.http_status()).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    (
        status,
        Json(json!({
            "code": error.code(),
            "message": error.message(),
        })),
    )
        .into_response()
}

fn urlencoding_minimal(value: &str) -> String {
    let mut out = String::new();
    for b in value.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

async fn security_headers(req: Request<Body>, next: Next) -> Response {
    let mut response = next.run(req).await;
    let headers = response.headers_mut();
    headers.insert(
        header::HeaderName::from_static("x-content-type-options"),
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        header::HeaderName::from_static("referrer-policy"),
        HeaderValue::from_static("no-referrer"),
    );
    headers.insert(
        header::HeaderName::from_static("cross-origin-resource-policy"),
        HeaderValue::from_static("same-origin"),
    );
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    headers.insert(
        header::HeaderName::from_static("content-security-policy"),
        HeaderValue::from_static(
            // Gallery and editor surfaces still ship inline bootstraps; 'unsafe-inline'
            // matches production-needed behavior until those scripts are externalized.
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-src 'self' blob:",
        ),
    );
    response
}

#[cfg(test)]
mod tests {
    use super::{inject_before_head_close, rewrite_fig_thumbs_for_project};

    #[test]
    fn inject_preserves_body_and_inserts_before_head_close() {
        let html = "<!doctype html><html><head><title>t</title></head><body><h1>hello-gallery</h1></body></html>";
        let out = inject_before_head_close(html, "<!--BOOT-->");
        assert!(out.contains("hello-gallery"), "original body lost: {out}");
        assert!(out.contains("<!--BOOT-->"));
        assert!(out.find("<!--BOOT-->").unwrap() < out.find("</head>").unwrap());
    }

    #[test]
    fn inject_prepends_when_no_head() {
        let html = "<h1>only-body</h1>";
        let out = inject_before_head_close(html, "<!--BOOT-->");
        assert!(out.starts_with("<!--BOOT-->"));
        assert!(out.contains("only-body"));
    }

    #[test]
    fn rewrite_scopes_fig_thumbs_without_double_prefix() {
        let key = "aaaaaaaaaaaaaaaaaaaaaaaa";
        let html = r#"<script src="/.fig_thumbs/agent_bridge_ui.js"></script>
<iframe src="/.fig_thumbs/code_editor.html"></iframe>
<link href="/p/aaaaaaaaaaaaaaaaaaaaaaaa/.fig_thumbs/editor.css">"#;
        let out = rewrite_fig_thumbs_for_project(html, key);
        assert!(out.contains(&format!("/p/{key}/.fig_thumbs/agent_bridge_ui.js")));
        assert!(out.contains(&format!("/p/{key}/.fig_thumbs/code_editor.html")));
        assert!(
            !out.contains(&format!("/p/{key}/p/{key}/")),
            "double-prefixed: {out}"
        );
        assert_eq!(
            out.matches(&format!("/p/{key}/.fig_thumbs/")).count(),
            3,
            "{out}"
        );
    }
}
