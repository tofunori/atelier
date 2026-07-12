//! Phase 2 smoke: daemon HTTP health + private control socket.

use std::{
    fs,
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    path::PathBuf,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

struct Daemon {
    child: Child,
    port: u16,
    state_dir: PathBuf,
}

impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = fs::remove_dir_all(&self.state_dir);
    }
}

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

fn start_daemon() -> Daemon {
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .unwrap();
    let binary = repo
        .join("rust/target/debug/atelier-daemon")
        .canonicalize()
        .expect("build atelier-daemon first");
    let port = free_port();
    let state_dir = std::env::temp_dir().join(format!(
        "atelier-daemon-smoke-{}-{}",
        std::process::id(),
        Instant::now().elapsed().as_nanos()
    ));
    fs::create_dir_all(&state_dir).unwrap();
    let child = Command::new(binary)
        .arg("--host")
        .arg("127.0.0.1")
        .arg("--port")
        .arg(port.to_string())
        .arg("--state-dir")
        .arg(&state_dir)
        .arg("--log-level")
        .arg("error")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn daemon");
    let daemon = Daemon {
        child,
        port,
        state_dir,
    };
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if let Ok(body) = http_get(daemon.port, "/healthz")
            && body.contains("\"ok\":true")
        {
            return daemon;
        }
        thread::sleep(Duration::from_millis(50));
    }
    panic!("daemon did not become ready on port {}", daemon.port);
}

fn http_get(port: u16, path: &str) -> Result<String, String> {
    let mut stream = std::net::TcpStream::connect(("127.0.0.1", port)).map_err(|e| e.to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .ok();
    let req = format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    stream.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
    let mut out = String::new();
    use std::io::Read;
    stream.read_to_string(&mut out).map_err(|e| e.to_string())?;
    Ok(out)
}

fn control_call(state_dir: &std::path::Path, method: &str, token: &str) -> serde_json::Value {
    let sock = state_dir.join("daemon.sock");
    let mut stream = UnixStream::connect(&sock).expect("connect control socket");
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .ok();
    let req = serde_json::json!({
        "id": "1",
        "protocol": 1,
        "method": method,
        "params": {},
        "token": token,
    });
    let mut line = req.to_string();
    line.push('\n');
    stream.write_all(line.as_bytes()).unwrap();
    let mut reader = BufReader::new(stream);
    let mut response = String::new();
    reader.read_line(&mut response).unwrap();
    serde_json::from_str(&response).unwrap()
}

#[test]
fn healthz_and_version_are_public() {
    let daemon = start_daemon();
    let health = http_get(daemon.port, "/healthz").unwrap();
    assert!(health.contains("200"));
    assert!(health.contains("atelier-daemon"));
    let version = http_get(daemon.port, "/version").unwrap();
    assert!(version.contains("daemonInstance"));
    assert!(version.contains("protocolVersion"));
}

#[test]
fn control_health_requires_token_and_survives_client_exit() {
    let daemon = start_daemon();
    let token = fs::read_to_string(daemon.state_dir.join("daemon.token"))
        .unwrap()
        .trim()
        .to_string();
    let denied = control_call(&daemon.state_dir, "daemon.health", "wrong");
    assert_eq!(denied["ok"], false);
    let ok = control_call(&daemon.state_dir, "daemon.health", &token);
    assert_eq!(ok["ok"], true);
    assert_eq!(ok["result"]["port"], daemon.port);
    // Client disconnect must not kill the daemon.
    let again = http_get(daemon.port, "/healthz").unwrap();
    assert!(again.contains("\"ok\":true"));
}

#[test]
fn control_methods_are_not_on_http() {
    let daemon = start_daemon();
    let body = http_get(daemon.port, "/daemon.health").unwrap();
    assert!(body.contains("404") || body.contains("405") || !body.contains("\"ok\":true"));
}
