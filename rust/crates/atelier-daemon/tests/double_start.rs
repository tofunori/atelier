//! P0: second daemon must not destroy the first instance's control socket.

use std::{
    fs,
    io::{Read, Write},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    thread,
    time::{Duration, Instant},
};

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

fn short_state_dir(prefix: &str) -> PathBuf {
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

fn binary() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../rust/target/debug/atelier-daemon")
        .canonicalize()
        .unwrap()
}

fn start_daemon(port: u16, state_dir: &PathBuf) -> Child {
    Command::new(binary())
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
        .stderr(Stdio::piped())
        .spawn()
        .unwrap()
}

fn wait_ready(port: u16, state_dir: &PathBuf) -> bool {
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if state_dir.join("daemon.sock").exists() && http_health(port) {
            return true;
        }
        thread::sleep(Duration::from_millis(40));
    }
    false
}

fn http_health(port: u16) -> bool {
    let Ok(mut stream) = std::net::TcpStream::connect(("127.0.0.1", port)) else {
        return false;
    };
    stream.set_read_timeout(Some(Duration::from_secs(1))).ok();
    let req = format!(
        "GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut out = String::new();
    let _ = stream.read_to_string(&mut out);
    out.contains("\"ok\":true")
}

fn control_health(state_dir: &PathBuf) -> bool {
    use std::io::{BufRead, BufReader};
    use std::os::unix::net::UnixStream;
    let token = match fs::read_to_string(state_dir.join("daemon.token")) {
        Ok(t) => t.trim().to_string(),
        Err(_) => return false,
    };
    let Ok(mut stream) = UnixStream::connect(state_dir.join("daemon.sock")) else {
        return false;
    };
    stream.set_read_timeout(Some(Duration::from_secs(1))).ok();
    let req = serde_json::json!({
        "id": "1",
        "protocol": 1,
        "method": "daemon.health",
        "params": {},
        "token": token,
    });
    let mut line = req.to_string();
    line.push('\n');
    if stream.write_all(line.as_bytes()).is_err() {
        return false;
    }
    let mut reader = BufReader::new(stream);
    let mut response = String::new();
    if reader.read_line(&mut response).is_err() {
        return false;
    }
    response.contains("\"ok\":true")
}

#[test]
fn second_start_does_not_break_first_control_socket() {
    let port = free_port();
    let state_dir = short_state_dir("dbl");
    let mut first = start_daemon(port, &state_dir);
    assert!(wait_ready(port, &state_dir), "first daemon not ready");
    assert!(
        control_health(&state_dir),
        "first control socket should answer"
    );

    // Second process: same state dir + same port must fail without damaging the first.
    let mut second = start_daemon(port, &state_dir);
    let status = second
        .wait_timeout()
        .expect("second process should exit");
    assert!(
        !status.success(),
        "second daemon must fail when instance is live"
    );

    // First remains healthy on HTTP and control.
    assert!(http_health(port), "first HTTP died after second start");
    assert!(
        control_health(&state_dir),
        "first control socket was destroyed by the second start"
    );
    assert!(
        state_dir.join("daemon.sock").exists(),
        "socket file missing after failed second start"
    );

    let _ = first.kill();
    let _ = first.wait();
}

/// Wait up to 3s for a child to exit; kill if it hangs (would mean it stole the port).
trait WaitTimeout {
    fn wait_timeout(&mut self) -> Option<std::process::ExitStatus>;
}

impl WaitTimeout for Child {
    fn wait_timeout(&mut self) -> Option<std::process::ExitStatus> {
        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline {
            if let Ok(Some(status)) = self.try_wait() {
                return Some(status);
            }
            thread::sleep(Duration::from_millis(50));
        }
        // Still running — kill it; that is also a failure mode for the test.
        let _ = self.kill();
        self.wait().ok()
    }
}
