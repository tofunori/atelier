//! `atelier daemon install|uninstall|start|stop|restart|status|doctor` (macOS launchd).

use crate::control_client;
use serde_json::json;
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};

const LABEL: &str = "io.atelier.daemon";

pub fn handle(args: &[String]) -> Result<(), String> {
    let sub = args.first().map(String::as_str).unwrap_or("status");
    match sub {
        "install" => install(),
        "uninstall" => uninstall(args.iter().any(|a| a == "--purge-data")),
        "start" => start(),
        "stop" => stop(),
        "restart" => restart(),
        "status" => status(args.iter().any(|a| a == "--json")),
        "doctor" => doctor(args.iter().any(|a| a == "--repair")),
        "logs" => logs(args.iter().any(|a| a == "--follow")),
        other => Err(format!("unknown daemon subcommand: {other}")),
    }
}

fn home() -> PathBuf {
    env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn state_dir() -> PathBuf {
    control_client::state_dir()
}

fn bin_dir() -> PathBuf {
    env::var_os("ATELIER_BIN_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join(".local/bin"))
}

fn assets_dir() -> PathBuf {
    env::var_os("ATELIER_ASSETS_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join(".local/share/atelier/assets"))
}

fn plist_path() -> PathBuf {
    home().join("Library/LaunchAgents/io.atelier.daemon.plist")
}

fn gui_domain() -> String {
    format!("gui/{}", unsafe { libc::getuid() })
}

fn which_or_sibling(name: &str) -> Result<PathBuf, String> {
    if let Ok(exe) = env::current_exe() {
        let candidate = exe.with_file_name(name);
        if candidate.is_file() {
            return Ok(candidate);
        }
    }
    let in_bin = bin_dir().join(name);
    if in_bin.is_file() {
        return Ok(in_bin);
    }
    let output = Command::new("which")
        .arg(name)
        .output()
        .map_err(|e| e.to_string())?;
    if output.status.success() {
        return Ok(PathBuf::from(
            String::from_utf8_lossy(&output.stdout).trim(),
        ));
    }
    Err(format!("{name} not found"))
}

fn render_plist(bin: &Path, state: &Path, assets: &Path, logs: &Path) -> String {
    let template = include_str!("../../../../packaging/io.atelier.daemon.plist");
    template
        .replace(
            "__ATELIER_BIN__",
            bin.parent().unwrap_or(bin).to_string_lossy().as_ref(),
        )
        .replace("__ATELIER_STATE__", state.to_string_lossy().as_ref())
        .replace("__ATELIER_ASSETS__", assets.to_string_lossy().as_ref())
        .replace("__ATELIER_LOGS__", logs.to_string_lossy().as_ref())
}

pub fn install() -> Result<(), String> {
    let daemon_bin = which_or_sibling("atelier-daemon")?;
    let state = state_dir();
    let assets = assets_dir();
    let logs = state.join("logs");
    fs::create_dir_all(&state).map_err(|e| e.to_string())?;
    fs::create_dir_all(&logs).map_err(|e| e.to_string())?;
    fs::create_dir_all(plist_path().parent().unwrap()).map_err(|e| e.to_string())?;
    let rendered = render_plist(&daemon_bin, &state, &assets, &logs);
    let plist = plist_path();
    fs::write(&plist, rendered).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&plist, fs::Permissions::from_mode(0o644)).ok();
    }
    let lint = Command::new("plutil")
        .args(["-lint", plist.to_str().unwrap_or("")])
        .status()
        .map_err(|e| e.to_string())?;
    if !lint.success() {
        return Err("plutil -lint failed for installed plist".into());
    }
    let domain = gui_domain();
    let _ = Command::new("launchctl")
        .args(["bootout", &format!("{domain}/{LABEL}")])
        .status();
    let bootstrap = Command::new("launchctl")
        .args(["bootstrap", &domain, plist.to_str().unwrap_or("")])
        .status()
        .map_err(|e| e.to_string())?;
    if !bootstrap.success() {
        return Err("launchctl bootstrap failed".into());
    }
    let _ = Command::new("launchctl")
        .args(["kickstart", "-k", &format!("{domain}/{LABEL}")])
        .status();
    println!(
        "{}",
        json!({
            "ok": true,
            "installed": true,
            "plist": plist,
            "binary": daemon_bin,
            "stateDir": state,
            "label": LABEL,
        })
    );
    Ok(())
}

pub fn uninstall(purge_data: bool) -> Result<(), String> {
    let domain = gui_domain();
    let _ = Command::new("launchctl")
        .args(["bootout", &format!("{domain}/{LABEL}")])
        .status();
    let plist = plist_path();
    if plist.is_file() {
        fs::remove_file(&plist).map_err(|e| e.to_string())?;
    }
    if purge_data {
        let state = state_dir();
        if state.is_dir() {
            fs::remove_dir_all(&state).map_err(|e| e.to_string())?;
        }
    }
    println!(
        "{}",
        json!({
            "ok": true,
            "uninstalled": true,
            "purgedData": purge_data,
            "label": LABEL,
        })
    );
    Ok(())
}

pub fn start() -> Result<(), String> {
    let domain = gui_domain();
    let status = Command::new("launchctl")
        .args(["kickstart", &format!("{domain}/{LABEL}")])
        .status()
        .map_err(|e| e.to_string())?;
    if !status.success() {
        return Err("launchctl kickstart failed (install first?)".into());
    }
    println!("{}", json!({"ok": true, "started": true, "label": LABEL}));
    Ok(())
}

pub fn stop() -> Result<(), String> {
    // Prefer clean shutdown via control socket (SuccessfulExit=false keeps it down).
    if control_client::call("daemon.shutdown", json!({"reason": "cli-stop"})).is_ok() {
        println!("{}", json!({"ok": true, "stopped": true, "via": "control"}));
        return Ok(());
    }
    let domain = gui_domain();
    let _ = Command::new("launchctl")
        .args(["bootout", &format!("{domain}/{LABEL}")])
        .status();
    println!("{}", json!({"ok": true, "stopped": true, "via": "bootout"}));
    Ok(())
}

pub fn restart() -> Result<(), String> {
    let domain = gui_domain();
    let status = Command::new("launchctl")
        .args(["kickstart", "-k", &format!("{domain}/{LABEL}")])
        .status()
        .map_err(|e| e.to_string())?;
    if !status.success() {
        return Err("launchctl kickstart -k failed".into());
    }
    println!("{}", json!({"ok": true, "restarted": true, "label": LABEL}));
    Ok(())
}

pub fn status(as_json: bool) -> Result<(), String> {
    let health = control_client::call("daemon.health", json!({}));
    let print = match health {
        Ok(value) => json!({"ok": true, "running": true, "health": value}),
        Err(error) => json!({"ok": false, "running": false, "error": error}),
    };
    let _ = as_json;
    println!("{print}");
    Ok(())
}

pub fn doctor(repair: bool) -> Result<(), String> {
    let mut issues = Vec::new();
    let daemon_bin = which_or_sibling("atelier-daemon");
    if daemon_bin.is_err() {
        issues.push("atelier-daemon binary missing");
    }
    if !plist_path().is_file() {
        issues.push("LaunchAgent plist missing");
        if repair {
            let _ = install();
        }
    }
    let health = control_client::call("daemon.health", json!({}));
    if health.is_err() {
        issues.push("daemon not reachable on control socket");
        if repair {
            let _ = start();
        }
    }
    println!(
        "{}",
        json!({
            "ok": issues.is_empty(),
            "issues": issues,
            "binary": daemon_bin.ok(),
            "plist": plist_path(),
            "stateDir": state_dir(),
            "health": health.ok(),
        })
    );
    Ok(())
}

pub fn logs(follow: bool) -> Result<(), String> {
    let path = state_dir().join("logs/bootstrap.log");
    if follow {
        let status = Command::new("tail")
            .args(["-f", path.to_str().unwrap_or("")])
            .status()
            .map_err(|e| e.to_string())?;
        if !status.success() {
            return Err("tail failed".into());
        }
    } else if path.is_file() {
        print!("{}", fs::read_to_string(path).map_err(|e| e.to_string())?);
    } else {
        println!("(no bootstrap.log yet at {})", path.display());
    }
    Ok(())
}
