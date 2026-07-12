//! Build and runtime identity for the daemon.

use serde::Serialize;
use std::time::Instant;

/// Protocol version for the Unix control socket JSON Lines API.
pub const PROTOCOL_VERSION: u32 = 1;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BuildInfo {
    pub version: String,
    pub git_sha: String,
    pub build_timestamp: String,
    pub target: String,
    pub protocol_version: u32,
    pub asset_hash: String,
    pub daemon_instance: String,
}

impl BuildInfo {
    pub fn current(asset_hash: impl Into<String>) -> Self {
        Self {
            version: env!("CARGO_PKG_VERSION").to_string(),
            git_sha: option_env!("ATELIER_GIT_SHA")
                .unwrap_or("unknown")
                .to_string(),
            build_timestamp: option_env!("ATELIER_BUILD_TIMESTAMP")
                .unwrap_or("unknown")
                .to_string(),
            target: option_env!("TARGET")
                .or(option_env!("CARGO_CFG_TARGET_TRIPLE"))
                .unwrap_or(std::env::consts::ARCH)
                .to_string(),
            protocol_version: PROTOCOL_VERSION,
            asset_hash: asset_hash.into(),
            daemon_instance: uuid::Uuid::new_v4().to_string(),
        }
    }
}

#[derive(Debug, Clone)]
pub struct RuntimeClock {
    started_at: Instant,
}

impl RuntimeClock {
    pub fn new() -> Self {
        Self {
            started_at: Instant::now(),
        }
    }

    pub fn uptime_secs(&self) -> u64 {
        self.started_at.elapsed().as_secs()
    }
}

impl Default for RuntimeClock {
    fn default() -> Self {
        Self::new()
    }
}
