use atelier_server::{LegacyServerConfig, run_legacy_server};
use clap::Parser;
use std::path::PathBuf;

#[derive(Parser, Debug, Clone)]
#[command(name = "atelier-server", about = "Portable Rust backend for Atelier")]
struct Args {
    #[arg(long, default_value = ".")]
    root: PathBuf,
    #[arg(long, default_value_t = 9360)]
    port: u16,
    #[arg(long, default_value = "127.0.0.1")]
    host: String,
    #[arg(long, default_value_t = true)]
    watch: bool,
    #[arg(long, hide = true)]
    no_watch: bool,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let args = Args::parse();
    run_legacy_server(LegacyServerConfig {
        root: args.root,
        port: args.port,
        host: args.host,
        watch: args.watch && !args.no_watch,
    })
    .await
}
