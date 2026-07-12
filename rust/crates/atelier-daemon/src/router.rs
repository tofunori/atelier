//! Public HTTP routes for the daemon (health/version only in Phase 2).

use crate::{
    build_info::{BuildInfo, RuntimeClock},
    config::DaemonConfig,
    registry::ProjectRegistry,
};
use axum::{
    Json, Router,
    extract::State,
    http::{HeaderValue, StatusCode, header},
    middleware::{self, Next},
    response::IntoResponse,
    routing::get,
};
use serde_json::{Value, json};
use std::sync::Arc;
use tower_http::trace::TraceLayer;

#[derive(Clone)]
pub struct DaemonHttpState {
    pub config: Arc<DaemonConfig>,
    pub build: Arc<BuildInfo>,
    pub clock: Arc<RuntimeClock>,
    pub registry: ProjectRegistry,
}

pub fn public_router(state: DaemonHttpState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/version", get(version))
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

async fn security_headers(
    req: axum::extract::Request,
    next: Next,
) -> axum::response::Response {
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
    headers.insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("no-store"),
    );
    headers.insert(
        header::HeaderName::from_static("content-security-policy"),
        HeaderValue::from_static(
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-src 'self' blob:",
        ),
    );
    response
}
