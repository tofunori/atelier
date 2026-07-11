//! atelier-cli — démarrage, diagnostic et contrôle du backend Rust.
//!
//! Commandes : serve, status, doctor, stop, run.
//! Le binaire `atelier-server` est cherché à côté du CLI, puis sur PATH.

use clap::{Parser, Subcommand};
use md5::{Digest, Md5};
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    net::TcpStream,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
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

#[derive(Parser, Debug)]
#[command(
    name = "atelier-cli",
    about = "CLI Rust for the Atelier backend (serve / status / doctor / stop / run)"
)]
struct Cli {
    #[command(subcommand)]
    command: CommandKind,
}

#[derive(Subcommand, Debug)]
enum CommandKind {
    /// Scan the project and generate figures_index.html + figures_data.json.
    Build {
        #[arg(long, default_value = ".")]
        root: PathBuf,
    },
    /// SVG editor utilities (re-apply manual deltas after regeneration).
    Svg {
        #[command(subcommand)]
        command: SvgCommand,
    },
    /// Start atelier-server in the foreground.
    Serve {
        #[arg(long, default_value = ".")]
        root: PathBuf,
        #[arg(long, default_value_t = 0)]
        port: u16,
        #[arg(long, default_value_t = true)]
        watch: bool,
        #[arg(long, hide = true)]
        no_watch: bool,
    },
    /// Print /ping JSON for a running server.
    Status {
        #[arg(long, default_value = ".")]
        root: PathBuf,
        #[arg(long, default_value_t = 0)]
        port: u16,
    },
    /// Verify the Rust backend answers /health.
    Doctor {
        #[arg(long, default_value = ".")]
        root: PathBuf,
        #[arg(long, default_value_t = 0)]
        port: u16,
        #[arg(long)]
        repair: bool,
    },
    /// Stop a server bound to the given port (best-effort via /ping + SIGTERM on owner pid is N/A;
    /// uses lsof/fuser on macOS when available).
    Stop {
        #[arg(long, default_value = ".")]
        root: PathBuf,
        #[arg(long, default_value_t = 0)]
        port: u16,
    },
    /// Start or reuse a detached server and optionally open the gallery.
    Run {
        #[arg(long, default_value = ".")]
        root: PathBuf,
        #[arg(long, default_value_t = 0)]
        port: u16,
        #[arg(long)]
        no_open: bool,
    },
    /// Build, start a detached server, and open the gallery.
    #[command(visible_alias = "view")]
    Open {
        #[arg(long, default_value = ".")]
        root: PathBuf,
        #[arg(long, default_value_t = 0)]
        port: u16,
        #[arg(long)]
        no_open: bool,
    },
    /// Build and host the server in the foreground once.
    Foreground {
        #[arg(long, default_value = ".")]
        root: PathBuf,
        #[arg(long, default_value_t = 0)]
        port: u16,
    },
}

#[derive(Subcommand, Debug)]
enum SvgCommand {
    /// Re-apply editor deltas from `.edits.json` onto a regenerated SVG.
    Reapply {
        /// Freshly regenerated SVG path.
        svg: PathBuf,
        /// Edits file (default: `<stem>.edits.json` next to the SVG).
        #[arg(long)]
        edits: Option<PathBuf>,
        /// Write result here (default: overwrite input).
        #[arg(long, short = 'o')]
        output: Option<PathBuf>,
        /// Print patched SVG to stdout instead of writing a file.
        #[arg(long)]
        stdout: bool,
    },
}

fn http_get(port: u16, path: &str) -> Result<(u16, String), String> {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).map_err(|error| error.to_string())?;
    stream.set_read_timeout(Some(Duration::from_secs(3))).ok();
    let request =
        format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    stream
        .write_all(request.as_bytes())
        .map_err(|error| error.to_string())?;
    let mut bytes = Vec::new();
    stream
        .read_to_end(&mut bytes)
        .map_err(|error| error.to_string())?;
    let response = String::from_utf8_lossy(&bytes);
    let (headers, body) = response.split_once("\r\n\r\n").unwrap_or(("", ""));
    let status = headers
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(0);
    Ok((status, body.to_string()))
}

fn project_root(requested: &Path) -> Result<PathBuf, String> {
    let requested = fs::canonicalize(requested).map_err(|error| error.to_string())?;
    let output = Command::new("git")
        .args([
            "-C",
            requested.to_str().unwrap_or("."),
            "rev-parse",
            "--show-toplevel",
        ])
        .output();
    if let Ok(output) = output
        && output.status.success()
    {
        let root = PathBuf::from(String::from_utf8_lossy(&output.stdout).trim());
        if root.is_dir() {
            return fs::canonicalize(root).map_err(|error| error.to_string());
        }
    }
    Ok(requested)
}

fn stable_port(root: &Path) -> u16 {
    let mut hash = Md5::new();
    hash.update(root.to_string_lossy().as_bytes());
    let digest = hash.finalize();
    8790 + (u128::from_be_bytes(digest.into()) % 1000) as u16
}

fn selected_port(root: &Path, requested: u16) -> u16 {
    if requested == 0 {
        stable_port(root)
    } else {
        requested
    }
}

fn state_path(root: &Path, port: u16) -> PathBuf {
    root.join(".fig_thumbs")
        .join(format!("atelier-server-{port}.json"))
}

fn live_atelier(port: u16, root: &Path) -> Option<Value> {
    let (200, body) = http_get(port, "/health").ok()? else {
        return None;
    };
    let payload: Value = serde_json::from_str(&body).ok()?;
    let project = payload.get("project").and_then(Value::as_str)?;
    (payload.get("backend") == Some(&json!("rust"))
        && fs::canonicalize(project).ok().as_deref() == Some(root))
    .then_some(payload)
}

fn write_state(root: &Path, port: u16, pid: u32) -> Result<(), String> {
    let path = state_path(root, port);
    let payload =
        json!({"service":"atelier","backend":"rust","project":root,"port":port,"pid":pid});
    atelier_core::atomic_write_json(&path, &payload).map_err(|error| error.to_string())
}

fn open_gallery(port: u16) {
    let url = format!("http://127.0.0.1:{port}/figures_index.html?nativeFs=1");
    if which("cmux").is_ok() {
        let _ = Command::new("cmux")
            .args(["browser", "open", &url])
            .status();
    } else {
        let _ = Command::new("open").arg(&url).status();
    }
    println!("atelier: gallery -> {url}");
}

fn find_server_binary() -> Result<PathBuf, String> {
    if let Ok(explicit) = std::env::var("ATELIER_RUST_SERVER") {
        let path = PathBuf::from(explicit);
        if path.is_file() {
            return Ok(path);
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        let sibling = exe.with_file_name("atelier-server");
        if sibling.is_file() {
            return Ok(sibling);
        }
    }
    if let Ok(path) = which("atelier-server") {
        return Ok(path);
    }
    Err("atelier-server not found (set ATELIER_RUST_SERVER or install next to atelier-cli)".into())
}

fn find_assets_dir() -> Result<PathBuf, String> {
    if let Ok(explicit) = std::env::var("ATELIER_ASSETS_DIR") {
        let path = PathBuf::from(explicit);
        if path.join("gallery_template.html").is_file() {
            return Ok(path);
        }
    }
    if let Ok(tool_root) = std::env::var("ATELIER_TOOL_ROOT") {
        let path = PathBuf::from(tool_root).join("assets");
        if path.join("gallery_template.html").is_file() {
            return Ok(path);
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        for path in [
            exe.parent().map(|p| p.join("../share/atelier/assets")),
            exe.parent().map(|p| p.join("../../share/atelier/assets")),
        ]
        .into_iter()
        .flatten()
        {
            if path.join("gallery_template.html").is_file() {
                return fs::canonicalize(path).map_err(|error| error.to_string());
            }
        }
    }
    Err("Atelier assets not found (set ATELIER_ASSETS_DIR)".into())
}

fn build_gallery(root: &Path) -> Result<(), String> {
    let root = fs::canonicalize(root).map_err(|error| error.to_string())?;
    let assets = find_assets_dir()?;
    let options = atelier_core::gallery_builder::GalleryBuildOptions {
        root: root.clone(),
        template: assets.join("gallery_template.html"),
        title: std::env::var("GALLERY_TITLE").unwrap_or_else(|_| "Atelier".into()),
        extensions: atelier_core::gallery_builder::parse_extensions(
            std::env::var("GALLERY_EXTS").ok().as_deref(),
        ),
        show_frames: std::env::var_os("GALLERY_SHOW_FRAMES").is_some(),
        no_thumbs: std::env::var_os("GALLERY_NO_THUMBS").is_some(),
    };
    let result =
        atelier_core::gallery_builder::build(&options).map_err(|error| error.to_string())?;
    println!(
        "atelier: {} files indexed -> {}",
        result.count,
        result.index.display()
    );
    Ok(())
}

fn which(bin: &str) -> Result<PathBuf, ()> {
    let output = Command::new("which").arg(bin).output().map_err(|_| ())?;
    if !output.status.success() {
        return Err(());
    }
    let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if path.is_empty() {
        return Err(());
    }
    Ok(PathBuf::from(path))
}

fn wait_up(port: u16, timeout: Duration) -> bool {
    let start = Instant::now();
    while start.elapsed() < timeout {
        if let Ok((200, _)) = http_get(port, "/ping") {
            return true;
        }
        thread::sleep(Duration::from_millis(150));
    }
    false
}

fn serve(root: &Path, port: u16, watch: bool) -> Result<(), String> {
    let root = project_root(root)?;
    let port = selected_port(&root, port);
    build_gallery(&root)?;
    let server = find_server_binary()?;
    let assets = find_assets_dir()?;
    let mut cmd = Command::new(&server);
    cmd.arg("--root")
        .arg(&root)
        .arg("--port")
        .arg(port.to_string());
    cmd.env("ATELIER_ASSETS_DIR", assets);
    if watch {
        cmd.arg("--watch");
    } else {
        cmd.arg("--no-watch");
    }
    let status = cmd
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .status()
        .map_err(|error| error.to_string())?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("server exited with {status}"))
    }
}

fn start_detached(root: &Path, requested_port: u16, open: bool) -> Result<(), String> {
    let root = project_root(root)?;
    let port = selected_port(&root, requested_port);
    build_gallery(&root)?;
    if live_atelier(port, &root).is_some() {
        println!("atelier: reusing Rust server on :{port}");
        if open {
            open_gallery(port);
        }
        return Ok(());
    }
    if http_get(port, "/ping").is_ok() {
        return Err(format!("port {port} is occupied by another service"));
    }
    let log = secure_log(&root, &format!("atelier-server-{port}.log"))?;
    let mut child = Command::new(find_server_binary()?)
        .args([
            "--root",
            root.to_str().unwrap_or("."),
            "--port",
            &port.to_string(),
            "--watch",
        ])
        .env("ATELIER_ASSETS_DIR", find_assets_dir()?)
        .stdin(Stdio::null())
        .stdout(Stdio::from(
            log.try_clone().map_err(|error| error.to_string())?,
        ))
        .stderr(Stdio::from(log))
        .spawn()
        .map_err(|error| error.to_string())?;
    if !wait_up(port, Duration::from_secs(15)) {
        let _ = child.kill();
        let _ = child.wait();
        return Err(format!("server did not answer /ping on :{port}"));
    }
    write_state(&root, port, child.id())?;
    println!(
        "atelier: detached Rust server pid {} on :{port}",
        child.id()
    );
    if open {
        open_gallery(port);
    }
    Ok(())
}

fn stop_project(root: &Path, requested_port: u16) -> Result<(), String> {
    let root = project_root(root)?;
    let port = selected_port(&root, requested_port);
    let state_file = state_path(&root, port);
    let state: Value = serde_json::from_slice(
        &fs::read(&state_file).map_err(|_| format!("no Atelier state for :{port}"))?,
    )
    .map_err(|error| error.to_string())?;
    let pid = state
        .get("pid")
        .and_then(Value::as_u64)
        .ok_or("invalid Atelier state")?;
    if live_atelier(port, &root).is_none() {
        return Err(format!("refusing to stop unverified process on :{port}"));
    }
    let listeners = Command::new("lsof")
        .args(["-nP", &format!("-iTCP:{port}"), "-sTCP:LISTEN", "-t"])
        .output()
        .map_err(|error| error.to_string())?;
    let expected = pid.to_string();
    if !listeners.status.success()
        || !String::from_utf8_lossy(&listeners.stdout)
            .split_whitespace()
            .any(|candidate| candidate == expected)
    {
        return Err(format!(
            "state PID {pid} is not the verified listener on :{port}"
        ));
    }
    let status = Command::new("kill")
        .args(["-TERM", &pid.to_string()])
        .status()
        .map_err(|error| error.to_string())?;
    if !status.success() {
        return Err(format!("failed to stop Atelier pid {pid}"));
    }
    let _ = fs::remove_file(state_file);
    println!("atelier: stopped pid {pid} on :{port}");
    Ok(())
}

fn svg_reapply(
    svg: PathBuf,
    edits: Option<PathBuf>,
    output: Option<PathBuf>,
    to_stdout: bool,
) -> Result<(), String> {
    if !svg.is_file() {
        return Err(format!("SVG not found: {}", svg.display()));
    }
    if to_stdout {
        let stem_edits = {
            let stem = svg.file_stem().and_then(|s| s.to_str()).unwrap_or("file");
            svg.parent()
                .unwrap_or_else(|| Path::new("."))
                .join(format!("{stem}.edits.json"))
        };
        let edits_file = edits.unwrap_or(stem_edits);
        if !edits_file.is_file() {
            eprintln!("no edits file ({}) — nothing to re-apply", edits_file.display());
            print!("{}", fs::read_to_string(&svg).map_err(|e| e.to_string())?);
            return Ok(());
        }
        let edits_val: Vec<serde_json::Value> = {
            let raw = fs::read_to_string(&edits_file).map_err(|e| e.to_string())?;
            let parsed: serde_json::Value =
                serde_json::from_str(&raw).map_err(|e| e.to_string())?;
            if let Some(arr) = parsed.get("edits").and_then(|v| v.as_array()) {
                arr.clone()
            } else if let Some(arr) = parsed.as_array() {
                arr.clone()
            } else {
                Vec::new()
            }
        };
        let raw = fs::read_to_string(&svg).map_err(|e| e.to_string())?;
        let (patched, report) = atelier_core::svg_edits::reapply(&raw, &edits_val);
        eprintln!(
            "re-applied {}/{} edit(s) ({} skipped, {} unmatched)",
            report.applied, report.total, report.skipped, report.missing
        );
        print!("{patched}");
        return Ok(());
    }
    let report = atelier_core::svg_edits::reapply_file(&svg, edits.as_deref(), output.as_deref())
        .map_err(|e| e.to_string())?;
    if report.total == 0 {
        eprintln!("no edits file — nothing to re-apply");
        return Ok(());
    }
    eprintln!(
        "re-applied {}/{} edit(s) ({} skipped, {} unmatched) → {}",
        report.applied,
        report.total,
        report.skipped,
        report.missing,
        output.as_ref().unwrap_or(&svg).display()
    );
    for detail in &report.missing_detail {
        eprintln!("  unmatched: {detail}");
    }
    Ok(())
}

fn main() -> Result<(), String> {
    match Cli::parse().command {
        CommandKind::Build { root } => build_gallery(&project_root(&root)?),
        CommandKind::Svg {
            command: SvgCommand::Reapply {
                svg,
                edits,
                output,
                stdout,
            },
        } => svg_reapply(svg, edits, output, stdout),
        CommandKind::Serve {
            root,
            port,
            watch,
            no_watch,
        } => loop {
            match serve(&root, port, watch && !no_watch) {
                Ok(()) => break Ok(()),
                Err(error) => {
                    eprintln!("atelier: server exited ({error}); restarting in 2s");
                    thread::sleep(Duration::from_secs(2));
                }
            }
        },
        CommandKind::Status { root, port } => {
            let root = project_root(&root)?;
            let port = selected_port(&root, port);
            let (status, body) = http_get(port, "/ping")?;
            if status != 200 {
                return Err(format!("HTTP {status}"));
            }
            println!("{body}");
            Ok(())
        }
        CommandKind::Doctor { root, port, repair } => {
            let root = project_root(&root)?;
            let port = selected_port(&root, port);
            if repair {
                build_gallery(&root)?;
                if http_get(port, "/ping").is_err() {
                    start_detached(&root, port, false)?;
                }
            }
            let (status, payload) =
                http_get(port, "/health").or_else(|_| http_get(port, "/ping"))?;
            if status != 200 {
                return Err(format!("HTTP {status} — is the server running on :{port}?"));
            }
            if payload.contains("\"backend\":\"rust\"") || payload.contains("\"backend\": \"rust\"")
            {
                println!("OK  rust backend  127.0.0.1:{port}");
            } else {
                println!("OK  server  127.0.0.1:{port}");
            }
            println!("OK  health  {payload}");
            Ok(())
        }
        CommandKind::Stop { root, port } => stop_project(&root, port),
        CommandKind::Run {
            root,
            port,
            no_open,
        } => start_detached(&root, port, !no_open),
        CommandKind::Open {
            root,
            port,
            no_open,
        } => start_detached(&root, port, !no_open),
        CommandKind::Foreground { root, port } => serve(&root, port, true),
    }
}
