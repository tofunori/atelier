//! Public HTTP routes for the daemon.

use crate::{
    build_info::{BuildInfo, RuntimeClock},
    config::DaemonConfig,
    host::{HostError, ProjectHost},
    registry::ProjectRegistry,
};
use atelier_server::legacy_project_router;
use axum::{
    Json, Router,
    body::Body,
    extract::{Request, State},
    http::{HeaderValue, StatusCode, Uri, header},
    middleware::{self, Next},
    response::{IntoResponse, Response},
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
}

pub fn public_router(state: DaemonHttpState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/version", get(version))
        // Phase 3: project surfaces under /p/{key}/... (sessions arrive in Phase 4).
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
        Ok(response) => response,
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": error.to_string()})),
        )
            .into_response(),
    }
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
