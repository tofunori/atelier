//! atelier-daemon — persistent multi-project Atelier runtime (Phase 2 skeleton).

mod build_info;
mod config;
mod control;
#[allow(dead_code)]
mod events;
#[allow(dead_code)]
mod lifecycle;
#[allow(dead_code)]
mod metrics;
mod registry;
mod router;
mod sessions;

use build_info::{BuildInfo, RuntimeClock};
use clap::Parser;
use config::DaemonConfig;
use control::{ControlState, ensure_private_state_dir, load_or_create_token, serve_control};
use registry::ProjectRegistry;
use router::{DaemonHttpState, public_router};
use sessions::SessionStore;
use std::{
    fs,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use tokio::{net::UnixListener, signal, sync::Notify};

#[derive(Parser, Debug)]
#[command(name = "atelier-daemon", about = "Persistent Atelier daemon")]
struct Args {
    #[arg(long, default_value_t = 9359)]
    port: u16,
    #[arg(long, default_value = "127.0.0.1")]
    host: String,
    #[arg(long)]
    state_dir: Option<PathBuf>,
    #[arg(long)]
    assets: Option<PathBuf>,
    #[arg(long)]
    control_socket: Option<PathBuf>,
    #[arg(long, default_value = "info")]
    log_level: String,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let args = Args::parse();
    let mut config = DaemonConfig::default();
    // File overlay (if present under default/state dir candidate).
    let state_candidate = args
        .state_dir
        .clone()
        .unwrap_or_else(|| config.state_dir.clone());
    if let Some(file) = DaemonConfig::load_file_overlay(&state_candidate.join("daemon.json"))? {
        config.apply_file(file);
    }
    config.apply_env();
    // CLI wins.
    config.host = args.host;
    config.port = args.port;
    config.log_level = args.log_level;
    if let Some(state_dir) = args.state_dir {
        config.state_dir = state_dir;
    }
    if let Some(assets) = args.assets {
        config.assets_dir = Some(assets);
    }
    config.validate().map_err(|error| -> Box<dyn std::error::Error + Send + Sync> {
        error.into()
    })?;

    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| {
                tracing_subscriber::EnvFilter::new(format!("atelier_daemon={}", config.log_level))
            }),
        )
        .with_writer(std::io::stderr)
        .init();

    ensure_private_state_dir(&config.state_dir)?;
    let token = load_or_create_token(&config.control_token_path())?;
    let asset_hash = config
        .assets_dir
        .as_ref()
        .and_then(|path| path.file_name())
        .and_then(|name| name.to_str())
        .unwrap_or("dev")
        .to_string();
    let build = Arc::new(BuildInfo::current(asset_hash));
    let clock = Arc::new(RuntimeClock::new());
    let config = Arc::new(config);
    let registry = ProjectRegistry::load(config.registry_path())?;
    let _sessions = SessionStore::empty(config.sessions_path());

    let control_path = args
        .control_socket
        .unwrap_or_else(|| config.control_socket_path());
    if control_path.exists() {
        let _ = fs::remove_file(&control_path);
    }
    let control_listener = UnixListener::bind(&control_path).map_err(|error| {
        format!(
            "failed to bind control socket {}: {error}",
            control_path.display()
        )
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&control_path, fs::Permissions::from_mode(0o600))?;
    }

    let address = format!("{}:{}", config.host, config.port);
    let http_listener = tokio::net::TcpListener::bind(&address)
        .await
        .map_err(|error| {
            if error.kind() == std::io::ErrorKind::AddrInUse {
                format!(
                    "port {} already in use on {} — another daemon or process owns it",
                    config.port, config.host
                )
            } else {
                format!("failed to bind HTTP {address}: {error}")
            }
        })?;

    let shutting_down = Arc::new(AtomicBool::new(false));
    let shutdown = Arc::new(Notify::new());
    let control_state = ControlState {
        config: config.clone(),
        build: build.clone(),
        clock: clock.clone(),
        token: Arc::new(token),
        shutting_down: shutting_down.clone(),
        shutdown: shutdown.clone(),
    };
    let http_state = DaemonHttpState {
        config: config.clone(),
        build: build.clone(),
        clock: clock.clone(),
        registry,
    };
    let app = public_router(http_state);

    tracing::info!(
        %address,
        control = %control_path.display(),
        instance = %build.daemon_instance,
        "atelier-daemon starting"
    );

    let control_task = {
        let state = control_state.clone();
        tokio::spawn(async move {
            serve_control(control_listener, state).await;
        })
    };

    let shutdown_signal = {
        let shutdown = shutdown.clone();
        let shutting_down = shutting_down.clone();
        async move {
            tokio::select! {
                _ = shutdown.notified() => {}
                _ = signal::ctrl_c() => {
                    shutting_down.store(true, Ordering::SeqCst);
                }
                _ = sigterm() => {
                    shutting_down.store(true, Ordering::SeqCst);
                }
            }
        }
    };

    // Serve until shutdown is requested, then allow at most 10s for connections to drain.
    let serve = axum::serve(http_listener, app).with_graceful_shutdown(shutdown_signal);
    let result = serve.await;
    shutting_down.store(true, Ordering::SeqCst);
    shutdown.notify_waiters();

    let drain = tokio::time::timeout(Duration::from_secs(10), control_task);
    if drain.await.is_err() {
        tracing::warn!("control loop did not exit within 10s");
    }
    let _ = fs::remove_file(&control_path);

    match result {
        Ok(()) => {
            tracing::info!("atelier-daemon stopped cleanly");
            Ok(())
        }
        Err(error) => Err(error.into()),
    }
}

#[cfg(unix)]
async fn sigterm() {
    if let Ok(mut stream) = signal::unix::signal(signal::unix::SignalKind::terminate()) {
        stream.recv().await;
    } else {
        std::future::pending::<()>().await;
    }
}

#[cfg(not(unix))]
async fn sigterm() {
    std::future::pending::<()>().await;
}
