//! JSON-Lines client for the persistent atelier-daemon control socket.

use serde_json::{Value, json};
use std::{
    env, fs,
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    path::PathBuf,
    time::Duration,
};

const PROTOCOL: u32 = 1;

pub fn runtime_mode() -> String {
    env::var("ATELIER_RUNTIME")
        .unwrap_or_else(|_| "auto".into())
        .to_ascii_lowercase()
}

/// Prefer the daemon when explicitly requested, or when the control socket is live.
/// `ATELIER_RUNTIME=legacy` forces the old per-project server spawn.
pub fn use_daemon() -> bool {
    match runtime_mode().as_str() {
        "legacy" => false,
        "daemon" => true,
        _ => default_state_dir().join("daemon.sock").exists(),
    }
}

pub fn default_state_dir() -> PathBuf {
    if let Ok(path) = env::var("ATELIER_DAEMON_STATE_DIR") {
        return PathBuf::from(path);
    }
    env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join("Library/Application Support/Atelier/daemon")
}

pub fn call(method: &str, params: Value) -> Result<Value, String> {
    let state_dir = default_state_dir();
    let sock = state_dir.join("daemon.sock");
    let token = fs::read_to_string(state_dir.join("daemon.token"))
        .map_err(|error| {
            format!(
                "atelier-daemon token missing at {}: {error} (is the daemon installed/running?)",
                state_dir.join("daemon.token").display()
            )
        })?
        .trim()
        .to_string();
    let mut stream = UnixStream::connect(&sock).map_err(|error| {
        format!(
            "cannot connect to atelier-daemon at {}: {error}",
            sock.display()
        )
    })?;
    stream.set_read_timeout(Some(Duration::from_secs(10))).ok();
    stream.set_write_timeout(Some(Duration::from_secs(10))).ok();
    let request = json!({
        "id": format!("mcp-{}", std::process::id()),
        "protocol": PROTOCOL,
        "method": method,
        "params": params,
        "token": token,
    });
    let mut line = request.to_string();
    line.push('\n');
    stream
        .write_all(line.as_bytes())
        .map_err(|error| error.to_string())?;
    let mut reader = BufReader::new(stream);
    let mut response = String::new();
    reader
        .read_line(&mut response)
        .map_err(|error| error.to_string())?;
    let value: Value = serde_json::from_str(&response)
        .map_err(|error| format!("invalid control response: {error}"))?;
    if value.get("ok") == Some(&json!(true)) {
        Ok(value.get("result").cloned().unwrap_or(json!({})))
    } else {
        let code = value
            .pointer("/error/code")
            .and_then(Value::as_str)
            .unwrap_or("ERROR");
        let message = value
            .pointer("/error/message")
            .and_then(Value::as_str)
            .unwrap_or("control call failed");
        if code == "VERSION_MISMATCH" {
            Err(format!(
                "VERSION_MISMATCH: {message} (set ATELIER_RUNTIME=legacy for the old server, or upgrade the daemon)"
            ))
        } else {
            Err(format!("{code}: {message}"))
        }
    }
}

pub fn project_open(root: &str, consumer: &str, label: Option<&str>) -> Result<Value, String> {
    let _ = call("project.register", json!({ "root": root }))?;
    let mut params = json!({
        "root": root,
        "consumer": consumer,
        "nativeFs": true,
        "theme": "Codex",
    });
    if let Some(label) = label {
        params["label"] = json!(label);
    }
    call("project.open", params)
}
