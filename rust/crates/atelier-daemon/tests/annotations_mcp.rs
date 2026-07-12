//! P0: daemon annotation bank contracts used by atelier-mcp (claim/ack/list/status).

use std::{
    fs,
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    thread,
    time::{Duration, Instant},
};

struct Daemon {
    child: Child,
    #[allow(dead_code)]
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
    static N: AtomicU64 = AtomicU64::new(0);
    let n = N.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("{prefix}{}-{n}", std::process::id() % 10_000));
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
    let state_dir = short_state_dir("ann");
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
    while Instant::now() < deadline {
        if state_dir.join("daemon.sock").exists()
            && UnixStream::connect(state_dir.join("daemon.sock")).is_ok()
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

fn make_project() -> PathBuf {
    static N: AtomicU64 = AtomicU64::new(0);
    let n = N.fetch_add(1, Ordering::SeqCst);
    let root = std::env::temp_dir().join(format!("ann-proj-{}-{}", std::process::id() % 10_000, n));
    fs::create_dir_all(&root).unwrap();
    fs::write(root.join("notes.md"), b"# notes\n").unwrap();
    fs::write(root.join("figures_index.html"), b"<html>x</html>\n").unwrap();
    fs::write(root.join("figures_data.json"), b"{\"files\":[]}\n").unwrap();
    root
}

#[test]
fn claim_ack_list_and_status_mutations() {
    let daemon = start_daemon();
    let root = make_project();
    let reg = control(
        &daemon,
        "project.register",
        serde_json::json!({ "root": root.to_string_lossy() }),
    );
    assert_eq!(reg["ok"], true, "{reg}");
    let key = reg["result"]["key"].as_str().unwrap().to_string();

    let consumer = "codex-thread-test-1";
    let dest = format!("thread:{consumer}");

    // Manual consumer registration preserves automatic=false.
    let cons = control(
        &daemon,
        "consumer.register",
        serde_json::json!({
            "key": key,
            "thread": consumer,
            "label": "Manual task",
            "mode": "manual",
        }),
    );
    assert_eq!(cons["ok"], true, "{cons}");
    assert_eq!(cons["result"]["automatic"], false, "{cons}");

    let auto = control(
        &daemon,
        "consumer.register",
        serde_json::json!({
            "key": key,
            "thread": "auto-consumer",
            "label": "Auto task",
            "mode": "automatic",
            "automatic": true,
        }),
    );
    assert_eq!(auto["ok"], true, "{auto}");
    assert_eq!(auto["result"]["automatic"], true, "{auto}");

    // Enqueue a deliverable annotation.
    let enq = control(
        &daemon,
        "annotation.enqueue",
        serde_json::json!({
            "key": key,
            "artifact": "notes.md",
            "comment": "please review",
            "destination": "auto",
        }),
    );
    assert_eq!(enq["ok"], true, "{enq}");
    let id = enq["result"]["id"].as_str().unwrap().to_string();

    // Bank list shows pending.
    let bank = control(
        &daemon,
        "annotation.bank",
        serde_json::json!({ "key": key, "limit": 20 }),
    );
    assert_eq!(bank["ok"], true, "{bank}");
    let pending = bank["result"]["pending"].as_array().unwrap();
    assert!(
        pending
            .iter()
            .any(|item| item.get("id").and_then(|v| v.as_str()) == Some(id.as_str())),
        "pending missing {id}: {bank}"
    );

    // Claim for consumer.
    let claim = control(
        &daemon,
        "annotation.claim",
        serde_json::json!({
            "key": key,
            "consumer": consumer,
            "destination": dest,
        }),
    );
    assert_eq!(claim["ok"], true, "{claim}");
    let items = claim["result"]["items"].as_array().unwrap();
    assert_eq!(items.len(), 1, "{claim}");
    assert_eq!(items[0]["id"], id);

    // Status mutation processing → completed.
    let processing = control(
        &daemon,
        "annotation.status",
        serde_json::json!({
            "key": key,
            "ids": [id],
            "status": "processing",
        }),
    );
    assert_eq!(processing["ok"], true, "{processing}");
    assert_eq!(processing["result"]["updated"], 1);

    let completed = control(
        &daemon,
        "annotation.status",
        serde_json::json!({
            "key": key,
            "ids": [id],
            "status": "completed",
            "result": "done",
        }),
    );
    assert_eq!(completed["ok"], true, "{completed}");

    // After completed, item leaves inbox; history keeps it.
    let bank2 = control(
        &daemon,
        "annotation.bank",
        serde_json::json!({ "key": key }),
    );
    let pending2 = bank2["result"]["pending"].as_array().unwrap();
    assert!(
        !pending2
            .iter()
            .any(|item| item.get("id").and_then(|v| v.as_str()) == Some(id.as_str())),
        "still pending after completed: {bank2}"
    );
    let history = bank2["result"]["history"].as_array().unwrap();
    let hist = history
        .iter()
        .find(|item| item.get("id").and_then(|v| v.as_str()) == Some(id.as_str()))
        .expect("history entry");
    assert_eq!(hist["status"], "completed");

    // Ack path: enqueue another, claim, ack.
    let enq2 = control(
        &daemon,
        "annotation.enqueue",
        serde_json::json!({
            "key": key,
            "artifact": "notes.md",
            "comment": "second",
            "destination": "auto",
        }),
    );
    let id2 = enq2["result"]["id"].as_str().unwrap().to_string();
    let _ = control(
        &daemon,
        "annotation.claim",
        serde_json::json!({
            "key": key,
            "consumer": consumer,
            "destination": dest,
        }),
    );
    let ack = control(
        &daemon,
        "annotation.ack",
        serde_json::json!({
            "key": key,
            "consumer": consumer,
            "ids": [id2],
        }),
    );
    assert_eq!(ack["ok"], true, "{ack}");
    assert!(ack["result"]["acked"].as_u64().unwrap_or(0) >= 1, "{ack}");

    let _ = fs::remove_dir_all(root);
}
