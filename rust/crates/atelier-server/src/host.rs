//! Phase 7 — intégrations hôte macOS (plein écran Orca, viewer natif).

use atelier_core::safe_project_path;
use axum::{
    Json,
    extract::State,
    http::{HeaderMap, StatusCode},
    response::IntoResponse,
};
use serde::Deserialize;
use serde_json::json;
use std::{path::PathBuf, process::Stdio};
use tokio::process::Command;

use crate::{AppState, request_allowed};

const NATIVE_FULLSCREEN_EXTS: &[&str] = &[
    "png", "jpg", "jpeg", "gif", "webp", "tif", "tiff", "bmp", "svg",
];

fn json_ok(payload: serde_json::Value) -> axum::response::Response {
    (StatusCode::OK, Json(payload)).into_response()
}

fn json_status(status: StatusCode, payload: serde_json::Value) -> axum::response::Response {
    (status, Json(payload)).into_response()
}

/// Deprecated compatibility no-op (same as Python).
pub async fn orca_fullscreen_exit(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> impl IntoResponse {
    if !request_allowed(&headers, &state) {
        return json_status(
            StatusCode::FORBIDDEN,
            json!({"ok": false, "error": "cross-origin blocked"}),
        );
    }
    json_ok(json!({
        "ok": true,
        "deprecated": true,
        "method": "noop; use /orca-native-fullscreen",
    }))
}

#[derive(Deserialize, Default)]
pub struct NativeFullscreenBody {
    rel: Option<String>,
}

pub async fn orca_native_fullscreen(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Option<Json<NativeFullscreenBody>>,
) -> impl IntoResponse {
    if !request_allowed(&headers, &state) {
        return json_status(
            StatusCode::FORBIDDEN,
            json!({"ok": false, "error": "cross-origin blocked"}),
        );
    }
    let rel = body.and_then(|Json(b)| b.rel).unwrap_or_default();
    let Ok(path) = safe_project_path(&state.root, &rel) else {
        return json_status(
            StatusCode::BAD_REQUEST,
            json!({"ok": false, "error": "not a supported project image"}),
        );
    };
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if !path.is_file() || !NATIVE_FULLSCREEN_EXTS.contains(&ext.as_str()) {
        return json_status(
            StatusCode::BAD_REQUEST,
            json!({"ok": false, "error": "not a supported project image"}),
        );
    }
    let Some(viewer) = native_viewer_path() else {
        return json_status(
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({"ok": false, "error": "native fullscreen viewer missing"}),
        );
    };
    let mut cmd = Command::new("python3");
    cmd.arg(&viewer)
        .arg(&path)
        .current_dir(&state.root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(false);
    #[cfg(unix)]
    unsafe {
        cmd.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }
    match cmd.spawn() {
        Ok(child) => {
            let pid = child.id().unwrap_or(0);
            // Detach: drop child without waiting (viewer runs independently).
            std::mem::forget(child);
            json_ok(json!({"ok": true, "pid": pid}))
        }
        Err(error) => json_status(
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({"ok": false, "error": error.to_string()}),
        ),
    }
}

fn native_viewer_path() -> Option<PathBuf> {
    if let Some(tool) = std::env::var_os("ATELIER_TOOL_ROOT") {
        let candidate = PathBuf::from(tool).join("native_fullscreen_viewer.py");
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    // Dev checkout: next to the server binary's source tree via CARGO_MANIFEST_DIR is
    // unavailable at runtime — try relative to current exe's ancestors.
    if let Ok(exe) = std::env::current_exe() {
        for ancestor in exe.ancestors().take(6) {
            let candidate = ancestor.join("native_fullscreen_viewer.py");
            if candidate.is_file() {
                return Some(candidate);
            }
            let candidate = ancestor.join("..").join("native_fullscreen_viewer.py");
            if let Ok(canon) = std::fs::canonicalize(&candidate)
                && canon.is_file()
            {
                return Some(canon);
            }
        }
    }
    None
}
