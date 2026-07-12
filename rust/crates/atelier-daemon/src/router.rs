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
    let Some(assets_dir) = state.config.assets_dir.as_ref() else {
        // Fallback: serve from ATELIER_ASSETS_DIR or sibling assets.
        let fallback = std::env::var_os("ATELIER_ASSETS_DIR")
            .map(std::path::PathBuf::from)
            .or_else(|| {
                std::env::current_exe()
                    .ok()
                    .and_then(|exe| exe.parent().map(|p| p.join("../share/atelier/assets")))
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
        Ok(response) => inject_bootstrap_if_html(response, &project_key, &state.build).await,
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": error.to_string()})),
        )
            .into_response(),
    }
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

    let Ok(bytes) = axum::body::to_bytes(body, 12 * 1024 * 1024).await else {
        return Response::from_parts(parts, Body::empty());
    };

    let looks_like_html = content_type.contains("text/html") || {
        let head = String::from_utf8_lossy(&bytes[..bytes.len().min(256)]).to_ascii_lowercase();
        head.contains("<!doctype html") || head.contains("<html")
    };

    if !looks_like_html {
        return Response::from_parts(parts, Body::from(bytes));
    }

    let original = String::from_utf8_lossy(&bytes);
    // Shared assets are served at /assets/* (hash segment is reserved for future immutable deploys).
    let asset_base = if build.asset_hash != "dev" && !build.asset_hash.is_empty() {
        format!("/assets/{}", build.asset_hash)
    } else {
        "/assets".to_string()
    };
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
    let injected = inject_before_head_close(&original, &injection);

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
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-src 'self' blob:",
        ),
    );
    response
}

#[cfg(test)]
mod tests {
    use super::inject_before_head_close;

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
}
