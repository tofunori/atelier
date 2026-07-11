//! atelier-cli — démarrage, diagnostic et contrôle du backend Rust.
//!
//! Commandes : serve, status, doctor, stop, run.
//! Le binaire `atelier-server` est cherché à côté du CLI, puis sur PATH.

use clap::{Parser, Subcommand};
use std::{
    io::{Read, Write},
    net::TcpStream,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

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
    /// Start atelier-server in the foreground.
    Serve {
        #[arg(long, default_value = ".")]
        root: PathBuf,
        #[arg(long, default_value_t = 9360)]
        port: u16,
        #[arg(long, default_value_t = true)]
        watch: bool,
        #[arg(long, hide = true)]
        no_watch: bool,
    },
    /// Print /ping JSON for a running server.
    Status {
        #[arg(long, default_value_t = 9360)]
        port: u16,
    },
    /// Verify the Rust backend answers /health.
    Doctor {
        #[arg(long, default_value_t = 9360)]
        port: u16,
    },
    /// Stop a server bound to the given port (best-effort via /ping + SIGTERM on owner pid is N/A;
    /// uses lsof/fuser on macOS when available).
    Stop {
        #[arg(long, default_value_t = 9360)]
        port: u16,
    },
    /// Build is delegated: just start the server after a short wait for /ping.
    Run {
        #[arg(long, default_value = ".")]
        root: PathBuf,
        #[arg(long, default_value_t = 9360)]
        port: u16,
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
    let server = find_server_binary()?;
    let mut cmd = Command::new(&server);
    cmd.arg("--root")
        .arg(root)
        .arg("--port")
        .arg(port.to_string());
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

fn stop_port(port: u16) -> Result<(), String> {
    // Prefer lsof on macOS: PIDs listening on the port.
    let output = Command::new("lsof")
        .args(["-nP", &format!("-iTCP:{port}"), "-sTCP:LISTEN", "-t"])
        .output();
    match output {
        Ok(out) if out.status.success() => {
            let pids = String::from_utf8_lossy(&out.stdout);
            let mut killed = 0;
            for pid in pids.split_whitespace() {
                if let Ok(status) = Command::new("kill").args(["-TERM", pid]).status()
                    && status.success()
                {
                    killed += 1;
                }
            }
            if killed == 0 {
                return Err(format!("no process killed on port {port}"));
            }
            println!("stopped {killed} process(es) on :{port}");
            Ok(())
        }
        _ => {
            // If nothing listens, treat as already stopped.
            if http_get(port, "/ping").is_err() {
                println!("no server on :{port}");
                return Ok(());
            }
            Err(format!(
                "could not stop server on :{port} (lsof unavailable or failed)"
            ))
        }
    }
}

fn main() -> Result<(), String> {
    match Cli::parse().command {
        CommandKind::Serve {
            root,
            port,
            watch,
            no_watch,
        } => serve(&root, port, watch && !no_watch),
        CommandKind::Status { port } => {
            let (status, body) = http_get(port, "/ping")?;
            if status != 200 {
                return Err(format!("HTTP {status}"));
            }
            println!("{body}");
            Ok(())
        }
        CommandKind::Doctor { port } => {
            let (status, payload) =
                http_get(port, "/health").or_else(|_| http_get(port, "/ping"))?;
            if status != 200 {
                return Err(format!("HTTP {status} — is the server running on :{port}?"));
            }
            if payload.contains("\"backend\":\"rust\"") || payload.contains("\"backend\": \"rust\"")
            {
                println!("OK  rust backend  127.0.0.1:{port}");
            } else if payload.contains("fig-annotate") {
                println!("WARN  python backend on :{port} (expected rust)");
            } else {
                println!("OK  server  127.0.0.1:{port}");
            }
            println!("OK  health  {payload}");
            Ok(())
        }
        CommandKind::Stop { port } => stop_port(port),
        CommandKind::Run { root, port } => {
            let server = find_server_binary()?;
            let mut child = Command::new(&server)
                .arg("--root")
                .arg(&root)
                .arg("--port")
                .arg(port.to_string())
                .arg("--watch")
                .stdin(Stdio::null())
                .stdout(Stdio::inherit())
                .stderr(Stdio::inherit())
                .spawn()
                .map_err(|error| error.to_string())?;
            if !wait_up(port, Duration::from_secs(15)) {
                let _ = child.kill();
                return Err(format!("server did not answer /ping on :{port}"));
            }
            println!("atelier-cli: rust backend on http://127.0.0.1:{port}/");
            let status = child.wait().map_err(|error| error.to_string())?;
            if status.success() {
                Ok(())
            } else {
                Err(format!("server exited with {status}"))
            }
        }
    }
}
