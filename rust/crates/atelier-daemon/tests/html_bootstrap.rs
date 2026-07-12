//! P0: project HTML must keep its body and include the Atelier bootstrap.

use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::net::UnixStream,
    path::PathBuf,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};
use uuid::Uuid;

struct Daemon {
    child: Child,
    port: u16,
    state_dir: PathBuf,
    token: String,
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

fn short_state_dir(prefix: &str) -> PathBuf {
    // Unix domain sockets on macOS have a ~104 byte path limit (SUN_LEN).
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    let n = N.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("{prefix}{}-{n}", std::process::id() % 10_000));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    dir
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .unwrap()
}

fn start_daemon() -> Daemon {
    let binary = repo_root()
        .join("rust/target/debug/atelier-daemon")
        .canonicalize()
        .expect("build atelier-daemon first");
    let port = free_port();
    let state_dir = short_state_dir("ad");
    fs::create_dir_all(&state_dir).unwrap();
    let assets = repo_root().join("assets");
    let mut child = Command::new(binary)
        .args([
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
            "--state-dir",
            state_dir.to_str().unwrap(),
            "--assets",
            assets.to_str().unwrap(),
            "--log-level",
            "error",
        ])
        .env("ATELIER_DAEMON_ALLOW_ANON", "1")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if state_dir.join("daemon.sock").exists()
            && http_get(port, "/healthz")
                .map(|b| b.contains("\"ok\":true"))
                .unwrap_or(false)
        {
            let token = fs::read_to_string(state_dir.join("daemon.token"))
                .unwrap()
                .trim()
                .to_string();
            return Daemon {
                child,
                port,
                state_dir,
                token,
            };
        }
        thread::sleep(Duration::from_millis(40));
    }
    let _ = child.kill();
    let _ = child.wait();
    panic!("daemon not ready");
}

fn http_get(port: u16, path: &str) -> Result<String, String> {
    let mut stream =
        std::net::TcpStream::connect(("127.0.0.1", port)).map_err(|e| e.to_string())?;
    stream.set_read_timeout(Some(Duration::from_secs(3))).ok();
    let req = format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    stream
        .write_all(req.as_bytes())
        .map_err(|e| e.to_string())?;
    let mut out = String::new();
    stream.read_to_string(&mut out).map_err(|e| e.to_string())?;
    Ok(out)
}

fn control(daemon: &Daemon, method: &str, params: serde_json::Value) -> serde_json::Value {
    let mut stream = UnixStream::connect(daemon.state_dir.join("daemon.sock")).unwrap();
    stream.set_read_timeout(Some(Duration::from_secs(3))).ok();
    let req = serde_json::json!({
        "id": "1",
        "protocol": 1,
        "method": method,
        "params": params,
        "token": daemon.token,
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
fn figures_index_keeps_html_and_bootstrap() {
    let daemon = start_daemon();
    let root = std::env::temp_dir().join(format!(
        "atelier-html-proj-{}-{}",
        std::process::id(),
        Uuid::new_v4()
    ));
    fs::create_dir_all(&root).unwrap();
    let marker = "UNIQUE_GALLERY_MARKER_42";
    fs::write(
        root.join("figures_index.html"),
        format!(
            "<!doctype html><html><head><title>gallery</title></head><body><h1>{marker}</h1></body></html>\n"
        ),
    )
    .unwrap();
    fs::write(root.join("figures_data.json"), b"{\"files\":[]}\n").unwrap();
    fs::write(root.join("notes.md"), b"# n\n").unwrap();

    let reg = control(
        &daemon,
        "project.register",
        serde_json::json!({ "root": root.to_string_lossy() }),
    );
    assert_eq!(reg["ok"], true, "{reg}");
    let key = reg["result"]["key"].as_str().unwrap();

    let response = http_get(daemon.port, &format!("/p/{key}/figures_index.html")).unwrap();
    assert!(
        response.contains("200") || response.to_ascii_lowercase().contains("200 ok"),
        "status: {response}"
    );
    let body = response.split("\r\n\r\n").nth(1).unwrap_or("");
    assert!(
        !body.is_empty(),
        "HTML body is empty — inject_bootstrap dropped content"
    );
    assert!(
        body.contains(marker),
        "original gallery HTML missing from body:\n{body}"
    );
    assert!(
        body.contains("id=\"atelier-bootstrap\""),
        "bootstrap script missing:\n{body}"
    );
    assert!(
        body.contains(key),
        "project key missing from bootstrap:\n{body}"
    );
    assert!(
        body.contains("atelier_runtime.js"),
        "runtime script tag missing:\n{body}"
    );

    let _ = fs::remove_dir_all(root);
}
