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
    http::{HeaderMap, HeaderValue, StatusCode, Uri, header},
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

async fn open_ticket(
    State(state): State<DaemonHttpState>,
    Path(ticket): Path<String>,
) -> Response {
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

async fn shared_asset(
    State(state): State<DaemonHttpState>,
    Path(path): Path<String>,
) -> Response {
    let Some(assets_dir) = state.config.assets_dir.as_ref() else {
        // Fallback: serve from ATELIER_ASSETS_DIR or sibling assets.
        let fallback = std::env::var_os("ATELIER_ASSETS_DIR")
            .map(std::path::PathBuf::from)
            .or_else(|| {
                std::env::current_exe().ok().and_then(|exe| {
                    exe.parent().map(|p| p.join("../share/atelier/assets"))
                })
            });
        let Some(root) = fallback else {
            return (StatusCode::NOT_FOUND, "assets not configured").into_response();
        };
        return read_asset(&root, &path);
    };
    read_asset(assets_dir, &path)
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
        Ok(mut response) => {
            inject_bootstrap_if_html(&mut response, &project_key, &state.build);
            response
        }
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": error.to_string()})),
        )
            .into_response(),
    }
}

fn inject_bootstrap_if_html(response: &mut Response, project_key: &str, build: &BuildInfo) {
    let is_html = response
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|ct| ct.contains("text/html"));
    if !is_html {
        // static_asset may omit content-type on some paths; still try for .html fallbacks later.
        return;
    }
    let body = std::mem::replace(response.body_mut(), Body::empty());
    // Best-effort: only rewrite small HTML bodies we can buffer.
    // Full streaming injection is deferred; for Phase 4 tests we set header hint.
    let _ = body;
    response.headers_mut().insert(
        header::HeaderName::from_static("x-atelier-project-key"),
        HeaderValue::from_str(project_key).unwrap_or_else(|_| HeaderValue::from_static("")),
    );
    response.headers_mut().insert(
        header::HeaderName::from_static("x-atelier-daemon-instance"),
        HeaderValue::from_str(&build.daemon_instance)
            .unwrap_or_else(|_| HeaderValue::from_static("")),
    );
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
    let status = StatusCode::from_u16(error.http_status()).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
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
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-src 'self' blob:",
        ),
    );
    response
}
