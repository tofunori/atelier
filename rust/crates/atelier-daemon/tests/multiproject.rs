//! Phase 3: two projects served on one daemon without cross-contamination.

use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::net::UnixStream,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

struct Daemon {
    child: Child,
    port: u16,
    state_dir: PathBuf,
    token: String,
    cleanup_state: bool,
}

impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        if self.cleanup_state {
            let _ = fs::remove_dir_all(&self.state_dir);
        }
    }
}

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
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
    let state_dir = std::env::temp_dir().join(format!(
        "atelier-multi-{}-{}",
        std::process::id(),
        Instant::now().elapsed().as_nanos()
    ));
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
        let sock_ready = state_dir.join("daemon.sock").exists()
            && UnixStream::connect(state_dir.join("daemon.sock")).is_ok();
        let http_ready =
            http_get(port, "/healthz").map(|b| b.contains("\"ok\":true")).unwrap_or(false);
        if sock_ready && http_ready {
            let token = fs::read_to_string(state_dir.join("daemon.token"))
                .unwrap()
                .trim()
                .to_string();
            return Daemon {
                child,
                port,
                state_dir,
                token,
                cleanup_state: true,
            };
        }
        thread::sleep(Duration::from_millis(40));
    }
    panic!("daemon not ready");
}

fn http_get(port: u16, path: &str) -> Result<String, String> {
    let mut stream = std::net::TcpStream::connect(("127.0.0.1", port)).map_err(|e| e.to_string())?;
    stream.set_read_timeout(Some(Duration::from_secs(3))).ok();
    let req = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
    let mut out = String::new();
    stream.read_to_string(&mut out).map_err(|e| e.to_string())?;
    Ok(out)
}

fn control(daemon: &Daemon, method: &str, params: serde_json::Value) -> serde_json::Value {
    let mut stream = UnixStream::connect(daemon.state_dir.join("daemon.sock")).unwrap();
    stream.set_read_timeout(Some(Duration::from_secs(3))).ok();
    let req = serde_json::json!({
        "id": "t",
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

fn make_project(label: &str, note: &str) -> PathBuf {
    let root = std::env::temp_dir().join(format!(
        "atelier-proj-{}-{}-{}",
        label,
        std::process::id(),
        Instant::now().elapsed().as_nanos()
    ));
    fs::create_dir_all(&root).unwrap();
    fs::write(root.join("notes.md"), note.as_bytes()).unwrap();
    fs::write(root.join("figures_data.json"), b"{\"files\":[]}\n").unwrap();
    fs::write(
        root.join("figures_index.html"),
        format!("<html><body>{label}</body></html>").as_bytes(),
    )
    .unwrap();
    // Shared filename to detect isolation bugs.
    fs::write(root.join("secret.txt"), format!("secret-for-{label}")).unwrap();
    root
}

#[test]
fn two_projects_are_isolated_on_same_port() {
    let daemon = start_daemon();
    let a = make_project("a", "# notes A\n");
    let b = make_project("b", "# notes B\n");

    let reg_a = control(
        &daemon,
        "project.register",
        serde_json::json!({ "root": a.to_string_lossy() }),
    );
    let reg_b = control(
        &daemon,
        "project.register",
        serde_json::json!({ "root": b.to_string_lossy() }),
    );
    assert_eq!(reg_a["ok"], true, "{reg_a}");
    assert_eq!(reg_b["ok"], true, "{reg_b}");
    let key_a = reg_a["result"]["key"].as_str().unwrap().to_string();
    let key_b = reg_b["result"]["key"].as_str().unwrap().to_string();
    assert_ne!(key_a, key_b);

    let notes_a = http_get(daemon.port, &format!("/p/{key_a}/notes/load")).unwrap();
    let notes_b = http_get(daemon.port, &format!("/p/{key_b}/notes/load")).unwrap();
    assert!(notes_a.contains("notes A"), "{notes_a}");
    assert!(notes_b.contains("notes B"), "{notes_b}");
    assert!(!notes_a.contains("notes B"));
    assert!(!notes_b.contains("notes A"));

    let secret_a = http_get(daemon.port, &format!("/p/{key_a}/raw?path=secret.txt")).unwrap();
    let secret_b = http_get(daemon.port, &format!("/p/{key_b}/raw?path=secret.txt")).unwrap();
    assert!(secret_a.contains("secret-for-a"), "{secret_a}");
    assert!(secret_b.contains("secret-for-b"), "{secret_b}");
    assert!(!secret_a.contains("secret-for-b"));
    assert!(!secret_b.contains("secret-for-a"));

    // Suspend A; B remains available.
    let sus = control(
        &daemon,
        "project.suspend",
        serde_json::json!({ "key": key_a }),
    );
    assert_eq!(sus["ok"], true, "{sus}");
    let still_b = http_get(daemon.port, &format!("/p/{key_b}/notes/load")).unwrap();
    assert!(still_b.contains("notes B"), "{still_b}");

    // Registry survives process restart.
    let state_dir = daemon.state_dir.clone();
    let port = free_port();
    // Keep state_dir; only stop the process.
    let mut daemon = daemon;
    daemon.cleanup_state = false;
    let _ = daemon.child.kill();
    let _ = daemon.child.wait();
    std::mem::forget(daemon);
    let binary = repo_root()
        .join("rust/target/debug/atelier-daemon")
        .canonicalize()
        .unwrap();
    let mut child = Command::new(binary)
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
    let mut ready = false;
    while Instant::now() < deadline {
        if http_get(port, "/healthz").map(|b| b.contains("\"ok\":true")).unwrap_or(false) {
            ready = true;
            break;
        }
        thread::sleep(Duration::from_millis(40));
    }
    assert!(ready, "restarted daemon not ready");
    let token = fs::read_to_string(state_dir.join("daemon.token"))
        .unwrap()
        .trim()
        .to_string();
    let mut stream = UnixStream::connect(state_dir.join("daemon.sock")).unwrap();
    let req = serde_json::json!({
        "id": "1",
        "protocol": 1,
        "method": "project.list",
        "params": {},
        "token": token,
    });
    let mut line = req.to_string();
    line.push('\n');
    stream.write_all(line.as_bytes()).unwrap();
    let mut reader = BufReader::new(stream);
    let mut response = String::new();
    reader.read_line(&mut response).unwrap();
    let list: serde_json::Value = serde_json::from_str(&response).unwrap();
    assert_eq!(list["ok"], true, "{list}");
    let projects = list["result"]["projects"].as_array().unwrap();
    assert_eq!(projects.len(), 2);
    let keys: Vec<&str> = projects
        .iter()
        .filter_map(|p| p.get("key").and_then(|k| k.as_str()))
        .collect();
    assert!(keys.contains(&key_a.as_str()));
    assert!(keys.contains(&key_b.as_str()));

    let _ = child.kill();
    let _ = child.wait();
    let _ = fs::remove_dir_all(&state_dir);
    let _ = fs::remove_dir_all(&a);
    let _ = fs::remove_dir_all(&b);
}

#[test]
fn unknown_project_key_is_404() {
    let daemon = start_daemon();
    let body = http_get(daemon.port, "/p/0123456789abcdef01234567/ping").unwrap();
    assert!(body.contains("404") || body.contains("PROJECT_NOT_FOUND"), "{body}");
}

#[allow(dead_code)]
fn assert_exists(path: &Path) {
    assert!(path.exists(), "{}", path.display());
}
