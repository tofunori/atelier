//! Lightweight process metrics for doctor/status.

use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ProcessMetrics {
    pub pid: u32,
    pub rss_bytes: Option<u64>,
}

impl ProcessMetrics {
    pub fn capture() -> Self {
        Self {
            pid: std::process::id(),
            rss_bytes: read_rss_bytes(),
        }
    }
}

#[cfg(target_os = "macos")]
fn read_rss_bytes() -> Option<u64> {
    use std::process::Command;
    let output = Command::new("ps")
        .args(["-o", "rss=", "-p", &std::process::id().to_string()])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let kb: u64 = text.trim().parse().ok()?;
    Some(kb.saturating_mul(1024))
}

#[cfg(not(target_os = "macos"))]
fn read_rss_bytes() -> Option<u64> {
    None
}
