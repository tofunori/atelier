mod daemon_client;

use md5::{Digest as Md5Digest, Md5};
use percent_encoding::{NON_ALPHANUMERIC, utf8_percent_encode};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    env, fs,
    io::{BufRead, BufReader, Read, Write},
    net::TcpStream,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{Mutex, OnceLock},
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[cfg(unix)]
use std::os::unix::fs::OpenOptionsExt;

fn secure_log(root: &Path, name: &str) -> Result<fs::File, String> {
    let root = fs::canonicalize(root).map_err(|error| error.to_string())?;
    let dir = root.join(".fig_thumbs");
    if dir.exists() {
        let metadata = fs::symlink_metadata(&dir).map_err(|error| error.to_string())?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err("unsafe .fig_thumbs directory".into());
        }
    } else {
        fs::create_dir(&dir).map_err(|error| error.to_string())?;
    }
    let dir = fs::canonicalize(dir).map_err(|error| error.to_string())?;
    if !dir.starts_with(&root) {
        return Err("unsafe log directory".into());
    }
    let mut options = fs::OpenOptions::new();
    options.create(true).append(true);
    #[cfg(unix)]
    options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
    options
        .open(dir.join(name))
        .map_err(|error| error.to_string())
}

struct Server {
    port: u16,
    token: String,
    child: Child,
}

static SERVERS: OnceLock<Mutex<HashMap<String, Server>>> = OnceLock::new();

fn servers() -> &'static Mutex<HashMap<String, Server>> {
    SERVERS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn canonical_root(value: Option<&Value>) -> Result<String, String> {
    let raw = value.and_then(Value::as_str).unwrap_or(".");
    fs::canonicalize(raw)
        .map(|path| path.to_string_lossy().to_string())
        .map_err(|error| format!("invalid project root: {error}"))
}

fn sibling(name: &str) -> Result<PathBuf, String> {
    if let Ok(path) = env::var(format!(
        "ATELIER_{}",
        name.to_ascii_uppercase().replace('-', "_")
    )) {
        let path = PathBuf::from(path);
        if path.is_file() {
            return Ok(path);
        }
    }
    let exe = env::current_exe().map_err(|error| error.to_string())?;
    let path = exe.with_file_name(name);
    if path.is_file() {
        return Ok(path);
    }
    which(name).ok_or_else(|| format!("{name} not found"))
}

fn which(name: &str) -> Option<PathBuf> {
    let output = Command::new("which").arg(name).output().ok()?;
    output
        .status
        .success()
        .then(|| PathBuf::from(String::from_utf8_lossy(&output.stdout).trim()))
}

fn assets_dir() -> Result<PathBuf, String> {
    if let Ok(path) = env::var("ATELIER_ASSETS_DIR") {
        let path = PathBuf::from(path);
        if path.join("gallery_template.html").is_file() {
            return Ok(path);
        }
    }
    let exe = env::current_exe().map_err(|error| error.to_string())?;
    for candidate in [
        exe.parent().map(|p| p.join("../share/atelier/assets")),
        exe.parent().map(|p| p.join("../../share/atelier/assets")),
    ]
    .into_iter()
    .flatten()
    {
        if candidate.join("gallery_template.html").is_file() {
            return fs::canonicalize(candidate).map_err(|error| error.to_string());
        }
    }
    Err("Atelier assets not found".into())
}

fn stable_port(root: &str) -> u16 {
    let mut hash = Md5::new();
    hash.update(root.as_bytes());
    let digest = hash.finalize();
    8790 + (u128::from_be_bytes(digest.into()) % 1000) as u16
}

fn token(root: &str) -> String {
    let mut hash = Sha256::new();
    hash.update(root);
    hash.update(std::process::id().to_le_bytes());
    hash.update(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
            .to_le_bytes(),
    );
    format!("{:x}", hash.finalize())
}

fn server_state_dir() -> PathBuf {
    env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join("Library/Application Support/Atelier/codex-servers")
}

fn root_key(root: &str) -> String {
    let mut hash = Sha256::new();
    hash.update(root.as_bytes());
    format!("{:x}", hash.finalize())[..20].to_string()
}

struct StartLock(PathBuf);

impl Drop for StartLock {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

fn acquire_start_lock(root: &str) -> Result<StartLock, String> {
    let dir = server_state_dir();
    fs::create_dir_all(&dir).map_err(|error| error.to_string())?;
    let path = dir.join(format!("{}.lock", root_key(root)));
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        let mut options = fs::OpenOptions::new();
        options.create_new(true).write(true);
        #[cfg(unix)]
        options.mode(0o600);
        match options.open(&path) {
            Ok(mut file) => {
                writeln!(file, "{}", std::process::id()).map_err(|error| error.to_string())?;
                return Ok(StartLock(path));
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                let stale = fs::metadata(&path)
                    .and_then(|metadata| metadata.modified())
                    .ok()
                    .and_then(|modified| modified.elapsed().ok())
                    .is_some_and(|age| age > Duration::from_secs(30));
                if stale {
                    let _ = fs::remove_file(&path);
                    continue;
                }
                if Instant::now() >= deadline {
                    return Err("timed out waiting for another Atelier startup".into());
                }
                thread::sleep(Duration::from_millis(50));
            }
            Err(error) => return Err(error.to_string()),
        }
    }
}

fn write_server_state(root: &str, port: u16, pid: u32, token: &str) -> Result<(), String> {
    let dir = server_state_dir();
    fs::create_dir_all(&dir).map_err(|error| error.to_string())?;
    let path = dir.join(format!("{}-{port}.json", root_key(root)));
    let mut options = fs::OpenOptions::new();
    options.create(true).truncate(true).write(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options.open(path).map_err(|error| error.to_string())?;
    serde_json::to_writer_pretty(
        &mut file,
        &json!({"service":"atelier-codex","project":root,"port":port,"pid":pid,"token":token,"protocol":2}),
    )
    .map_err(|error| error.to_string())?;
    file.write_all(b"\n").map_err(|error| error.to_string())
}

fn discover_server(root: &str) -> Option<(u16, String, u32, PathBuf)> {
    let dir = server_state_dir();
    let prefix = format!("{}-", root_key(root));
    let expected_port = stable_port(root);
    for entry in fs::read_dir(&dir).ok()?.flatten() {
        let path = entry.path();
        let name = path.file_name()?.to_string_lossy();
        if !name.starts_with(&prefix) || !name.ends_with(".json") {
            continue;
        }
        let value: Value = serde_json::from_slice(&fs::read(&path).ok()?).ok()?;
        let port = value.get("port")?.as_u64()? as u16;
        let pid = value.get("pid")?.as_u64()? as u32;
        let saved_root = value.get("project")?.as_str()?;
        let saved_token = value.get("token")?.as_str()?.to_string();
        let alive = unsafe { libc::kill(pid as libc::pid_t, 0) == 0 };
        let verified = alive
            && saved_root == root
            && http(port, "GET", "/ping", None, None)
                .ok()
                .is_some_and(|payload| {
                    payload.get("backend") == Some(&json!("rust"))
                        && payload.get("project").and_then(Value::as_str) == Some(root)
                        && payload.get("agentHost") == Some(&json!("codex"))
                        && payload.get("agentBridgeProtocol") == Some(&json!(2))
                });
        if verified {
            if port == expected_port {
                return Some((port, saved_token, pid, path));
            }
            // Pre-0.1.6 Codex integrations chose an ephemeral port. Retire a
            // verified legacy instance so every task converges on the stable
            // project URL below. Only Atelier processes for this exact root
            // reach this branch.
            unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
            let _ = fs::remove_file(path);
            continue;
        }
        let _ = fs::remove_file(path);
    }
    None
}

fn build(root: &str) -> Result<(), String> {
    let cli = sibling("atelier-cli")?;
    let status = Command::new(cli)
        .args(["build", "--root", root])
        .env("ATELIER_ASSETS_DIR", assets_dir()?)
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .status()
        .map_err(|error| error.to_string())?;
    status
        .success()
        .then_some(())
        .ok_or_else(|| "atelier build failed".into())
}

fn ensure_server(root: &str) -> Result<(u16, String), String> {
    if let Some(server) = servers().lock().unwrap().get_mut(root)
        && server.child.try_wait().ok().flatten().is_none()
    {
        return Ok((server.port, server.token.clone()));
    }
    if let Some((port, token, _, _)) = discover_server(root) {
        return Ok((port, token));
    }
    let _start_lock = acquire_start_lock(root)?;
    if let Some((port, token, _, _)) = discover_server(root) {
        return Ok((port, token));
    }
    build(root)?;
    let port = stable_port(root);
    if http(port, "GET", "/ping", None, None).is_ok() {
        return Err(format!(
            "stable Atelier port {port} is occupied by another service"
        ));
    }
    let token = token(root);
    let server_bin = sibling("atelier-server")?;
    let log = secure_log(Path::new(root), &format!("atelier-codex-{port}.log"))?;
    let mut child = Command::new(server_bin)
        .args(["--root", root, "--port", &port.to_string(), "--watch"])
        .env("ATELIER_ASSETS_DIR", assets_dir()?)
        .env("ATELIER_AGENT_HOST", "codex")
        .env("ATELIER_AGENT_TOKEN", &token)
        .stdin(Stdio::null())
        .stdout(Stdio::from(
            log.try_clone().map_err(|error| error.to_string())?,
        ))
        .stderr(Stdio::from(log))
        .spawn()
        .map_err(|error| error.to_string())?;
    let deadline = Instant::now() + Duration::from_secs(15);
    while Instant::now() < deadline {
        let alive = child.try_wait().ok().flatten().is_none();
        let owns_port = Command::new("lsof")
            .args(["-nP", &format!("-iTCP:{port}"), "-sTCP:LISTEN", "-t"])
            .output()
            .is_ok_and(|output| {
                output.status.success()
                    && String::from_utf8_lossy(&output.stdout)
                        .split_whitespace()
                        .any(|pid| pid == child.id().to_string())
            });
        let verified = http(port, "GET", "/ping", None, None)
            .ok()
            .is_some_and(|payload| {
                payload.get("backend") == Some(&json!("rust"))
                    && payload.get("project").and_then(Value::as_str) == Some(root)
                    && payload.get("agentHost") == Some(&json!("codex"))
                    && payload.get("agentBridgeProtocol") == Some(&json!(2))
            });
        if alive && owns_port && verified {
            write_server_state(root, port, child.id(), &token)?;
            servers().lock().unwrap().insert(
                root.into(),
                Server {
                    port,
                    token: token.clone(),
                    child,
                },
            );
            return Ok((port, token));
        }
        thread::sleep(Duration::from_millis(100));
    }
    let _ = child.kill();
    let _ = child.wait();
    Err("atelier-server did not answer /ping".into())
}

fn http(
    port: u16,
    method: &str,
    path: &str,
    body: Option<&Value>,
    token: Option<&str>,
) -> Result<Value, String> {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).map_err(|error| error.to_string())?;
    stream.set_read_timeout(Some(Duration::from_secs(60))).ok();
    let encoded = body
        .map(|value| serde_json::to_vec(value).unwrap_or_default())
        .unwrap_or_default();
    let mut request = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: application/json\r\nConnection: close\r\n"
    );
    if !encoded.is_empty() {
        request.push_str(&format!(
            "Content-Type: application/json\r\nContent-Length: {}\r\n",
            encoded.len()
        ));
    }
    if let Some(token) = token {
        request.push_str(&format!("Authorization: Bearer {token}\r\n"));
    }
    request.push_str("\r\n");
    stream
        .write_all(request.as_bytes())
        .map_err(|error| error.to_string())?;
    stream
        .write_all(&encoded)
        .map_err(|error| error.to_string())?;
    let mut response = Vec::new();
    stream
        .read_to_end(&mut response)
        .map_err(|error| error.to_string())?;
    let marker = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or("invalid HTTP response")?;
    let headers = String::from_utf8_lossy(&response[..marker]);
    let status = headers
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|v| v.parse::<u16>().ok())
        .unwrap_or(0);
    let payload = serde_json::from_slice(&response[marker + 4..]).unwrap_or_else(|_| json!({}));
    if !(200..300).contains(&status) {
        return Err(format!("HTTP {status}: {payload}"));
    }
    Ok(payload)
}

fn encoded(value: &str) -> String {
    utf8_percent_encode(value, NON_ALPHANUMERIC).to_string()
}

fn register(root: &str, label: Option<&str>, automatic: bool) -> Result<Value, String> {
    let (port, token) = ensure_server(root)?;
    let thread_id = env::var("CODEX_THREAD_ID")
        .ok()
        .filter(|id| !id.trim().is_empty());
    let consumer = thread_id
        .clone()
        .unwrap_or_else(|| format!("codex-{}", std::process::id()));
    let destination = thread_id
        .map(|id| format!("thread:{id}"))
        .unwrap_or_else(|| consumer.clone());
    http(
        port,
        "POST",
        "/agent-consumers/register",
        Some(&json!({
            "consumer": consumer,
            "destination": destination,
            "label": label.unwrap_or("Codex task"), "automatic": automatic,
        })),
        Some(&token),
    )
}


fn consumer_id() -> String {
    env::var("CODEX_THREAD_ID").unwrap_or_else(|_| format!("codex-{}", std::process::id()))
}

fn call_daemon(name: &str, root: &str, args: &Value) -> Result<Value, String> {
    match name {
        "atelier_open" => {
            let consumer = consumer_id();
            let opened = daemon_client::project_open(
                root,
                &consumer,
                args.get("label").and_then(Value::as_str),
            )?;
            let key = opened.get("key").and_then(Value::as_str).unwrap_or_default();
            let url = opened.get("url").and_then(Value::as_str).unwrap_or_default();
            let open_url = opened
                .get("openUrl")
                .and_then(Value::as_str)
                .unwrap_or(url);
            let _ = daemon_client::call(
                "consumer.register",
                json!({
                    "key": key,
                    "thread": consumer,
                    "label": args.get("label").and_then(Value::as_str).unwrap_or("Codex task"),
                    "mode": if args.get("automatic").and_then(Value::as_bool).unwrap_or(false) {
                        "automatic"
                    } else {
                        "manual"
                    },
                }),
            );
            Ok(json!({
                "ok": true,
                "serverReady": true,
                "panelVisible": false,
                "runtime": "daemon",
                "project": root,
                "projectKey": key,
                "url": url,
                "openUrl": open_url,
                "nextAction": "Open openUrl in the visible Codex browser; after redirect the final URL must match url. Keep that tab as a deliverable before reporting success."
            }))
        }
        "atelier_connect" => {
            let consumer = consumer_id();
            let opened = daemon_client::project_open(root, &consumer, args.get("label").and_then(Value::as_str))?;
            let key = opened.get("key").cloned().unwrap_or(json!(null));
            daemon_client::call(
                "consumer.register",
                json!({
                    "key": key,
                    "thread": consumer,
                    "label": args.get("label").and_then(Value::as_str).unwrap_or("Codex task"),
                    "mode": "manual",
                }),
            )
        }
        "atelier_get_selection" => {
            let consumer = consumer_id();
            let key = daemon_client::call("project.register", json!({"root": root}))?
                .get("key")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            daemon_client::call(
                "annotation.claim",
                json!({
                    "key": key,
                    "consumer": consumer,
                    "destination": format!("thread:{consumer}"),
                }),
            )
        }
        "atelier_wait_for_annotation" => {
            let seconds = args
                .get("timeoutSeconds")
                .and_then(Value::as_f64)
                .unwrap_or(30.0)
                .clamp(1.0, 55.0);
            let deadline = Instant::now() + Duration::from_secs_f64(seconds);
            loop {
                let value = call_daemon("atelier_get_selection", root, args)?;
                if value
                    .get("items")
                    .and_then(Value::as_array)
                    .is_some_and(|items| !items.is_empty())
                    || Instant::now() >= deadline
                {
                    break Ok(value);
                }
                thread::sleep(Duration::from_millis(350));
            }
        }
        "atelier_ack_selection" => {
            let consumer = consumer_id();
            let key = daemon_client::call("project.register", json!({"root": root}))?
                .get("key")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            daemon_client::call(
                "annotation.ack",
                json!({
                    "key": key,
                    "consumer": consumer,
                    "ids": args.get("ids").cloned().unwrap_or(json!([])),
                }),
            )
        }
        "atelier_list_annotations" => {
            let key = daemon_client::call("project.register", json!({"root": root}))?
                .get("key")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            let limit = args
                .get("limit")
                .and_then(Value::as_u64)
                .unwrap_or(50);
            daemon_client::call(
                "annotation.bank",
                json!({ "key": key, "limit": limit }),
            )
        }
        "atelier_set_annotation_status" => {
            let key = daemon_client::call("project.register", json!({"root": root}))?
                .get("key")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            let ids = args.get("ids").cloned().unwrap_or(json!([]));
            let status = args
                .get("status")
                .cloned()
                .unwrap_or(json!("processing"));
            let result = args.get("result").cloned().unwrap_or(json!(""));
            let error = args.get("error").cloned().unwrap_or(json!(""));
            daemon_client::call(
                "annotation.status",
                json!({
                    "key": key,
                    "ids": ids,
                    "status": status,
                    "result": result,
                    "error": error,
                }),
            )
        }
        "atelier_rescan" => {
            let key = daemon_client::call("project.register", json!({"root": root}))?
                .get("key")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            daemon_client::call("project.rescan", json!({ "key": key }))
        }
        "atelier_mark_updated" => {
            let key = daemon_client::call("project.register", json!({"root": root}))?
                .get("key")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            daemon_client::call(
                "project.event",
                json!({
                    "key": key,
                    "rel": args.get("path"),
                    "note": args.get("note"),
                }),
            )
        }
        "atelier_stop" => {
            // Daemon is process-global; stop does not kill it. Suspend project instead.
            let key = daemon_client::call("project.register", json!({"root": root}))?
                .get("key")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            let _ = daemon_client::call("project.suspend", json!({ "key": key }));
            Ok(json!({
                "ok": true,
                "stopped": false,
                "suspended": true,
                "runtime": "daemon",
                "project": root,
                "note": "atelier-daemon stays running; project runtime suspended"
            }))
        }
        _ => Err(format!("unknown tool: {name}")),
    }
}

fn call(name: &str, args: &Value) -> Result<Value, String> {
    let root = canonical_root(args.get("root"))?;
    if daemon_client::use_daemon() {
        return call_daemon(name, &root, args);
    }
    match name {
        "atelier_open" => {
            let (port, _) = ensure_server(&root)?;
            let _ = register(
                &root,
                args.get("label").and_then(Value::as_str),
                args.get("automatic")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
            );
            Ok(json!({
                "ok": true,
                "serverReady": true,
                "panelVisible": false,
                "project": root,
                "port": port,
                "url": format!("http://127.0.0.1:{port}/figures_index.html?nativeFs=1&theme=Codex"),
                "nextAction": "Open the returned openUrl (or url after redirect) in the visible Codex in-app browser and keep that tab as a deliverable before reporting success."
            }))
        }
        "atelier_connect" => register(
            &root,
            args.get("label").and_then(Value::as_str),
            args.get("automatic")
                .and_then(Value::as_bool)
                .unwrap_or(false),
        ),
        "atelier_get_selection" => {
            let (port, token) = ensure_server(&root)?;
            let consumer = env::var("CODEX_THREAD_ID")
                .unwrap_or_else(|_| format!("codex-{}", std::process::id()));
            http(
                port,
                "GET",
                &format!("/agent-selections?consumer={}", encoded(&consumer)),
                None,
                Some(&token),
            )
        }
        "atelier_wait_for_annotation" => {
            let seconds = args
                .get("timeoutSeconds")
                .and_then(Value::as_f64)
                .unwrap_or(30.0)
                .clamp(1.0, 55.0);
            let deadline = Instant::now() + Duration::from_secs_f64(seconds);
            loop {
                let value = call("atelier_get_selection", args)?;
                if value
                    .get("items")
                    .and_then(Value::as_array)
                    .is_some_and(|items| !items.is_empty())
                    || Instant::now() >= deadline
                {
                    break Ok(value);
                }
                thread::sleep(Duration::from_millis(350));
            }
        }
        "atelier_ack_selection" => {
            let (port, token) = ensure_server(&root)?;
            let consumer = env::var("CODEX_THREAD_ID")
                .unwrap_or_else(|_| format!("codex-{}", std::process::id()));
            http(
                port,
                "POST",
                "/agent-selections/ack",
                Some(&json!({
                    "ids": args.get("ids").cloned().unwrap_or(json!([])),
                    "consumer": consumer,
                })),
                Some(&token),
            )
        }
        "atelier_list_annotations" => {
            let (port, token) = ensure_server(&root)?;
            http(port, "GET", "/agent-status", None, Some(&token))
        }
        "atelier_set_annotation_status" => {
            let (port, token) = ensure_server(&root)?;
            http(
                port,
                "POST",
                "/agent-annotations/status",
                Some(args),
                Some(&token),
            )
        }
        "atelier_rescan" => {
            let (port, token) = ensure_server(&root)?;
            http(port, "POST", "/rescan", Some(&json!({})), Some(&token))
        }
        "atelier_mark_updated" => {
            let (port, token) = ensure_server(&root)?;
            http(
                port,
                "POST",
                "/agent-event",
                Some(&json!({"rel": args.get("path"), "note": args.get("note")})),
                Some(&token),
            )
        }
        "atelier_stop" => {
            if let Some(mut server) = servers().lock().unwrap().remove(&root) {
                let _ = server.child.kill();
                let _ = server.child.wait();
                if let Some((_, _, _, path)) = discover_server(&root) {
                    let _ = fs::remove_file(path);
                }
                Ok(json!({"ok": true, "stopped": true, "port": server.port, "project": root}))
            } else if let Some((port, _, pid, path)) = discover_server(&root) {
                unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
                let _ = fs::remove_file(path);
                Ok(json!({"ok": true, "stopped": true, "port": port, "project": root}))
            } else {
                Ok(json!({"ok": true, "stopped": false, "project": root}))
            }
        }
        _ => Err(format!("unknown tool: {name}")),
    }
}

fn tools() -> Vec<Value> {
    let root = json!({"type":"string"});
    [
        ("atelier_open", "Start or reuse Atelier and return its exact local URL. This tool does not open or confirm a visible Codex panel; after it returns, open that URL in the visible in-app browser and keep the tab as a deliverable before reporting success.", json!({"root":root,"label":{"type":"string"},"automatic":{"type":"boolean"}}), vec!["root"]),
        ("atelier_connect", "Pair this Codex task with Atelier.", json!({"root":root,"label":{"type":"string"},"automatic":{"type":"boolean"}}), vec!["root"]),
        ("atelier_get_selection", "Read annotations sent from Atelier.", json!({"root":root}), vec!["root"]),
        ("atelier_wait_for_annotation", "Wait for an Atelier annotation.", json!({"root":root,"timeoutSeconds":{"type":"number","minimum":1,"maximum":55}}), vec!["root"]),
        ("atelier_ack_selection", "Acknowledge received annotations.", json!({"root":root,"ids":{"type":"array","items":{"type":"string"}}}), vec!["root","ids"]),
        ("atelier_list_annotations", "List pending and historical annotations.", json!({"root":root,"limit":{"type":"integer"},"label":{"type":"string"}}), vec!["root"]),
        ("atelier_set_annotation_status", "Update annotation status.", json!({"root":root,"ids":{"type":"array"},"status":{"type":"string"},"result":{"type":"string"},"error":{"type":"string"}}), vec!["root","ids","status"]),
        ("atelier_rescan", "Rebuild the running gallery.", json!({"root":root}), vec!["root"]),
        ("atelier_mark_updated", "Notify that an artifact changed.", json!({"root":root,"path":{"type":"string"},"note":{"type":"string"}}), vec!["root","path"]),
        ("atelier_stop", "Stop the Atelier server.", json!({"root":root}), vec!["root"]),
    ]
    .into_iter()
    .map(|(name, description, properties, required)| json!({"name":name,"description":description,"inputSchema":{"type":"object","properties":properties,"required":required}}))
    .collect()
}

fn reply(id: Value, result: Value) {
    println!("{}", json!({"jsonrpc":"2.0","id":id,"result":result}));
}

fn main() {
    for line in BufReader::new(std::io::stdin())
        .lines()
        .map_while(Result::ok)
    {
        let Ok(request) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        let id = request.get("id").cloned().unwrap_or(Value::Null);
        match request
            .get("method")
            .and_then(Value::as_str)
            .unwrap_or_default()
        {
            "initialize" => reply(
                id,
                json!({"protocolVersion":request.pointer("/params/protocolVersion").cloned().unwrap_or(json!("2025-03-26")),"capabilities":{"tools":{}},"serverInfo":{"name":"atelier","version":"0.2.0"}}),
            ),
            "tools/list" => reply(id, json!({"tools":tools()})),
            "tools/call" => {
                let name = request
                    .pointer("/params/name")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                let args = request
                    .pointer("/params/arguments")
                    .cloned()
                    .unwrap_or_else(|| json!({}));
                match call(name, &args) {
                    Ok(value) => reply(
                        id,
                        json!({"content":[{"type":"text","text":value.to_string()}],"structuredContent":value}),
                    ),
                    Err(error) => reply(
                        id,
                        json!({"content":[{"type":"text","text":error}],"isError":true}),
                    ),
                }
            }
            _ if !id.is_null() => println!(
                "{}",
                json!({"jsonrpc":"2.0","id":id,"error":{"code":-32601,"message":"unknown method"}})
            ),
            _ => {}
        }
        let _ = std::io::stdout().flush();
    }
    // Servers deliberately outlive one MCP process. A new Codex task discovers
    // the 0600 state file and reuses the same local server instantly.
}

#[cfg(test)]
mod tests {
    use super::stable_port;

    #[test]
    fn project_port_is_stable_and_in_the_reserved_range() {
        let root = "/Users/tofunori/Documents/cmux-gallery";
        let port = stable_port(root);
        assert_eq!(port, stable_port(root));
        assert!((8790..9790).contains(&port));
    }
}
