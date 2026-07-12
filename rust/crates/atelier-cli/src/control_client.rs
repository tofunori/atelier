//! Shared JSON-Lines client for atelier-daemon.

use serde_json::{Value, json};
use std::{
    env, fs,
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    path::PathBuf,
    time::Duration,
};

const PROTOCOL: u32 = 1;

pub fn use_daemon() -> bool {
    match env::var("ATELIER_RUNTIME")
        .unwrap_or_else(|_| "auto".into())
        .to_ascii_lowercase()
        .as_str()
    {
        "legacy" => false,
        "daemon" => true,
        _ => state_dir().join("daemon.sock").exists(),
    }
}

pub fn state_dir() -> PathBuf {
    if let Ok(path) = env::var("ATELIER_DAEMON_STATE_DIR") {
        return PathBuf::from(path);
    }
    env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join("Library/Application Support/Atelier/daemon")
}

pub fn call(method: &str, params: Value) -> Result<Value, String> {
    let dir = state_dir();
    let sock = dir.join("daemon.sock");
    let token = fs::read_to_string(dir.join("daemon.token"))
        .map_err(|error| format!("daemon token missing: {error}"))?
        .trim()
        .to_string();
    let mut stream =
        UnixStream::connect(&sock).map_err(|error| format!("daemon socket: {error}"))?;
    stream.set_read_timeout(Some(Duration::from_secs(10))).ok();
    let request = json!({
        "id": format!("cli-{}", std::process::id()),
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
    let value: Value =
        serde_json::from_str(&response).map_err(|error| format!("bad control JSON: {error}"))?;
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
            .unwrap_or("failed");
        Err(format!("{code}: {message}"))
    }
}
