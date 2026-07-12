//! Phase 4: open tickets + project-scoped cookies.

use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
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
    let dir = std::env::temp_dir().join(format!(
        "{prefix}{}-{n}",
        std::process::id() % 10_000
    ));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    dir
}

fn start_daemon() -> Daemon {
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .unwrap();
    let binary = repo
        .join("rust/target/debug/atelier-daemon")
        .canonicalize()
        .unwrap();
    let port = free_port();
    let state_dir = short_state_dir("ad");
    fs::create_dir_all(&state_dir).unwrap();
    let child = Command::new(binary)
        .args([
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
            "--state-dir",
            state_dir.to_str().unwrap(),
            "--log-level",
            "error",
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if state_dir.join("daemon.sock").exists()
            && http_raw(port, "GET", "/healthz", None, None)
                .map(|r| r.contains("\"ok\":true"))
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
    panic!("daemon not ready");
}

fn http_raw(
    port: u16,
    method: &str,
    path: &str,
    cookie: Option<&str>,
    extra_headers: Option<&str>,
) -> Result<String, String> {
    let mut stream = std::net::TcpStream::connect(("127.0.0.1", port)).map_err(|e| e.to_string())?;
    stream.set_read_timeout(Some(Duration::from_secs(3))).ok();
    let mut req = format!("{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n");
    if let Some(cookie) = cookie {
        req.push_str(&format!("Cookie: {cookie}\r\n"));
    }
    if let Some(extra) = extra_headers {
        req.push_str(extra);
    }
    req.push_str("\r\n");
    stream.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
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
fn open_ticket_sets_cookie_and_rejects_replay() {
    let daemon = start_daemon();
    let root = std::env::temp_dir().join(format!(
        "atelier-sess-proj-{}-{}",
        std::process::id(),
        Instant::now().elapsed().as_nanos()
    ));
    fs::create_dir_all(&root).unwrap();
    fs::write(root.join("notes.md"), b"# s\n").unwrap();
    fs::write(root.join("figures_index.html"), b"<html>ok</html>\n").unwrap();
    fs::write(root.join("figures_data.json"), b"{\"files\":[]}\n").unwrap();

    let open = control(
        &daemon,
        "project.open",
        serde_json::json!({ "root": root.to_string_lossy(), "consumer": "task-1" }),
    );
    assert_eq!(open["ok"], true, "{open}");
    let key = open["result"]["key"].as_str().unwrap();
    let open_url = open["result"]["openUrl"].as_str().unwrap();
    let ticket = open_url.rsplit('/').next().unwrap();

    // Without session: 401
    let denied = http_raw(daemon.port, "GET", &format!("/p/{key}/notes/load"), None, None).unwrap();
    assert!(denied.contains("401") || denied.contains("SESSION_INVALID"), "{denied}");

    // Consume ticket
    let consumed = http_raw(daemon.port, "GET", &format!("/open/{ticket}"), None, None).unwrap();
    let consumed_l = consumed.to_ascii_lowercase();
    assert!(
        consumed_l.contains("302") || consumed_l.contains("307") || consumed_l.contains("location:"),
        "{consumed}"
    );
    assert!(consumed_l.contains("set-cookie: atelier_session="), "{consumed}");
    let cookie_line = consumed
        .lines()
        .find(|l| l.to_ascii_lowercase().starts_with("set-cookie: atelier_session="))
        .expect("set-cookie header");
    let session = cookie_line
        .split("atelier_session=")
        .nth(1)
        .unwrap()
        .split(';')
        .next()
        .unwrap();

    // With session: allowed
    let ok = http_raw(
        daemon.port,
        "GET",
        &format!("/p/{key}/notes/load"),
        Some(&format!("atelier_session={session}")),
        None,
    )
    .unwrap();
    assert!(ok.contains("200"), "{ok}");
    assert!(ok.contains("# s") || ok.contains("notes"), "{ok}");

    // Replay ticket fails
    let replay = http_raw(daemon.port, "GET", &format!("/open/{ticket}"), None, None).unwrap();
    assert!(replay.contains("401") || replay.contains("SESSION_INVALID"), "{replay}");

    // Cross-project cookie rejected
    let root_b = std::env::temp_dir().join(format!(
        "atelier-sess-proj-b-{}-{}",
        std::process::id(),
        Instant::now().elapsed().as_nanos()
    ));
    fs::create_dir_all(&root_b).unwrap();
    fs::write(root_b.join("notes.md"), b"# b\n").unwrap();
    fs::write(root_b.join("figures_index.html"), b"<html>b</html>\n").unwrap();
    fs::write(root_b.join("figures_data.json"), b"{\"files\":[]}\n").unwrap();
    let open_b = control(
        &daemon,
        "project.open",
        serde_json::json!({ "root": root_b.to_string_lossy() }),
    );
    let key_b = open_b["result"]["key"].as_str().unwrap();
    let cross = http_raw(
        daemon.port,
        "GET",
        &format!("/p/{key_b}/notes/load"),
        Some(&format!("atelier_session={session}")),
        None,
    )
    .unwrap();
    assert!(cross.contains("401") || cross.contains("SESSION_INVALID"), "{cross}");

    let _ = fs::remove_dir_all(root);
    let _ = fs::remove_dir_all(root_b);
}
