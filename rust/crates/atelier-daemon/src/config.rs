//! Daemon configuration: CLI > env > daemon.json > defaults.

use serde::{Deserialize, Serialize};
use std::{
    fs,
    net::IpAddr,
    path::{Path, PathBuf},
};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DaemonConfig {
    pub host: String,
    pub port: u16,
    pub state_dir: PathBuf,
    pub assets_dir: Option<PathBuf>,
    pub idle_grace_seconds: u64,
    pub suspend_after_seconds: u64,
    pub ticket_ttl_seconds: u64,
    pub session_ttl_seconds: u64,
    pub max_chrome_jobs: usize,
    pub max_latex_jobs: usize,
    pub max_full_scans: usize,
    pub max_browser_sessions: usize,
    pub max_sse_connections: usize,
    pub max_request_body_bytes: usize,
    pub log_level: String,
}

impl Default for DaemonConfig {
    fn default() -> Self {
        Self {
            host: "127.0.0.1".into(),
            port: 9359,
            state_dir: default_state_dir(),
            assets_dir: None,
            idle_grace_seconds: 300,
            suspend_after_seconds: 1800,
            ticket_ttl_seconds: 30,
            session_ttl_seconds: 2_592_000,
            max_chrome_jobs: 2,
            max_latex_jobs: 2,
            max_full_scans: 2,
            max_browser_sessions: 500,
            max_sse_connections: 256,
            max_request_body_bytes: 1_048_576,
            log_level: "info".into(),
        }
    }
}

impl DaemonConfig {
    pub fn validate(&self) -> Result<(), String> {
        let host: IpAddr = self
            .host
            .parse()
            .map_err(|error| format!("invalid host: {error}"))?;
        if !host.is_loopback() {
            return Err(format!(
                "host must be loopback only (got {}); remote binds are refused",
                self.host
            ));
        }
        if !(1024..=65535).contains(&self.port) {
            return Err(format!("port must be in 1024..=65535 (got {})", self.port));
        }
        if self.idle_grace_seconds < 60 || self.idle_grace_seconds > 3600 {
            return Err("idleGraceSeconds must be in 60..=3600".into());
        }
        if self.suspend_after_seconds <= self.idle_grace_seconds {
            return Err("suspendAfterSeconds must be greater than idleGraceSeconds".into());
        }
        if self.ticket_ttl_seconds < 5 || self.ticket_ttl_seconds > 120 {
            return Err("ticketTtlSeconds must be in 5..=120".into());
        }
        if self.session_ttl_seconds < 3600 || self.session_ttl_seconds > 7_776_000 {
            return Err("sessionTtlSeconds must be in 3600..=7776000".into());
        }
        if !(1..=8).contains(&self.max_chrome_jobs) {
            return Err("maxChromeJobs must be in 1..=8".into());
        }
        if !(1..=8).contains(&self.max_latex_jobs) {
            return Err("maxLatexJobs must be in 1..=8".into());
        }
        if !(1..=4).contains(&self.max_full_scans) {
            return Err("maxFullScans must be in 1..=4".into());
        }
        if !(50..=2000).contains(&self.max_browser_sessions) {
            return Err("maxBrowserSessions must be in 50..=2000".into());
        }
        if !(32..=1024).contains(&self.max_sse_connections) {
            return Err("maxSseConnections must be in 32..=1024".into());
        }
        if !(65_536..=10_485_760).contains(&self.max_request_body_bytes) {
            return Err("maxRequestBodyBytes must be in 65536..=10485760".into());
        }
        match self.log_level.as_str() {
            "trace" | "debug" | "info" | "warn" | "error" => {}
            other => return Err(format!("invalid logLevel: {other}")),
        }
        Ok(())
    }

    pub fn control_socket_path(&self) -> PathBuf {
        self.state_dir.join("daemon.sock")
    }

    pub fn control_token_path(&self) -> PathBuf {
        self.state_dir.join("daemon.token")
    }

    pub fn registry_path(&self) -> PathBuf {
        self.state_dir.join("registry.json")
    }

    pub fn sessions_path(&self) -> PathBuf {
        self.state_dir.join("sessions.json")
    }

    #[allow(dead_code)]
    pub fn daemon_json_path(&self) -> PathBuf {
        self.state_dir.join("daemon.json")
    }

    #[allow(dead_code)]
    pub fn logs_dir(&self) -> PathBuf {
        self.state_dir.join("logs")
    }

    pub fn load_file_overlay(path: &Path) -> Result<Option<DaemonConfigFile>, String> {
        if !path.is_file() {
            return Ok(None);
        }
        let raw = fs::read_to_string(path).map_err(|error| error.to_string())?;
        let file: DaemonConfigFile =
            serde_json::from_str(&raw).map_err(|error| format!("invalid daemon.json: {error}"))?;
        Ok(Some(file))
    }

    pub fn apply_file(&mut self, file: DaemonConfigFile) {
        if let Some(host) = file.host {
            self.host = host;
        }
        if let Some(port) = file.port {
            self.port = port;
        }
        if let Some(state_dir) = file.state_dir {
            self.state_dir = expand_user_path(&state_dir);
        }
        if let Some(assets_dir) = file.assets_dir {
            self.assets_dir = Some(expand_user_path(&assets_dir));
        }
        if let Some(value) = file.idle_grace_seconds {
            self.idle_grace_seconds = value;
        }
        if let Some(value) = file.suspend_after_seconds {
            self.suspend_after_seconds = value;
        }
        if let Some(value) = file.ticket_ttl_seconds {
            self.ticket_ttl_seconds = value;
        }
        if let Some(value) = file.session_ttl_seconds {
            self.session_ttl_seconds = value;
        }
        if let Some(value) = file.max_chrome_jobs {
            self.max_chrome_jobs = value;
        }
        if let Some(value) = file.max_latex_jobs {
            self.max_latex_jobs = value;
        }
        if let Some(value) = file.max_full_scans {
            self.max_full_scans = value;
        }
        if let Some(value) = file.max_browser_sessions {
            self.max_browser_sessions = value;
        }
        if let Some(value) = file.max_sse_connections {
            self.max_sse_connections = value;
        }
        if let Some(value) = file.max_request_body_bytes {
            self.max_request_body_bytes = value;
        }
        if let Some(value) = file.log_level {
            self.log_level = value;
        }
    }

    pub fn apply_env(&mut self) {
        if let Ok(host) = std::env::var("ATELIER_DAEMON_HOST") {
            self.host = host;
        }
        if let Ok(port) = std::env::var("ATELIER_DAEMON_PORT")
            && let Ok(port) = port.parse()
        {
            self.port = port;
        }
        if let Ok(state_dir) = std::env::var("ATELIER_DAEMON_STATE_DIR") {
            self.state_dir = expand_user_path(&state_dir);
        }
        if let Ok(assets) = std::env::var("ATELIER_DAEMON_ASSETS_DIR") {
            self.assets_dir = Some(expand_user_path(&assets));
        }
        if let Ok(level) = std::env::var("ATELIER_DAEMON_LOG_LEVEL") {
            self.log_level = level;
        }
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DaemonConfigFile {
    pub host: Option<String>,
    pub port: Option<u16>,
    pub state_dir: Option<String>,
    pub assets_dir: Option<String>,
    pub idle_grace_seconds: Option<u64>,
    pub suspend_after_seconds: Option<u64>,
    pub ticket_ttl_seconds: Option<u64>,
    pub session_ttl_seconds: Option<u64>,
    pub max_chrome_jobs: Option<usize>,
    pub max_latex_jobs: Option<usize>,
    pub max_full_scans: Option<usize>,
    pub max_browser_sessions: Option<usize>,
    pub max_sse_connections: Option<usize>,
    pub max_request_body_bytes: Option<usize>,
    pub log_level: Option<String>,
}

pub fn default_state_dir() -> PathBuf {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    home.join("Library/Application Support/Atelier/daemon")
}

fn expand_user_path(raw: &str) -> PathBuf {
    if let Some(rest) = raw.strip_prefix("~/") {
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
        return home.join(rest);
    }
    if raw == "~" {
        return std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
    }
    PathBuf::from(raw)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_non_loopback_host() {
        let cfg = DaemonConfig {
            host: "0.0.0.0".into(),
            ..Default::default()
        };
        assert!(cfg.validate().unwrap_err().contains("loopback"));
    }

    #[test]
    fn accepts_default_config() {
        DaemonConfig::default().validate().unwrap();
    }
}
