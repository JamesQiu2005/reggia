//! Docker Desktop integration: detection, compose up/down.

use std::process::{Command, Stdio};

use serde::Serialize;
use tauri::AppHandle;

use crate::config;

#[derive(Debug, Serialize)]
pub struct DockerStatus {
    pub installed: bool,
    pub running: bool,
}

pub fn check_status() -> DockerStatus {
    let installed = which("docker").is_some();
    let running = installed
        && Command::new("docker")
            .args(["ps", "-q"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
    DockerStatus { installed, running }
}

fn which(bin: &str) -> Option<std::path::PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let candidate = dir.join(bin);
        if candidate.is_file() {
            return Some(candidate);
        }
        #[cfg(windows)]
        {
            let exe = dir.join(format!("{bin}.exe"));
            if exe.is_file() {
                return Some(exe);
            }
        }
    }
    None
}

/// Run `docker compose -f <bundled compose> up -d` with required env.
/// The bundled compose file uses `${REGGIA_WORKSPACE}` for the volume mount
/// and `${DEEPSEEK_API_KEY}` for the API key.
pub fn compose_up(app: &AppHandle, cfg: &config::UserConfig) -> Result<(), String> {
    let compose = app
        .path_resolver()
        .resolve_resource("resources/compose/docker-compose.yml")
        .ok_or("could not locate bundled docker-compose.yml")?;
    let workspace = config::workspace_path(app);

    let status = Command::new("docker")
        .args([
            "compose",
            "-f",
            compose.to_str().ok_or("compose path is not utf-8")?,
            "up",
            "-d",
        ])
        .env("DEEPSEEK_API_KEY", &cfg.deepseek_api_key)
        .env(
            "REGGIA_WORKSPACE",
            workspace.to_str().ok_or("workspace path is not utf-8")?,
        )
        .status()
        .map_err(|e| format!("spawn docker compose: {e}"))?;

    if !status.success() {
        return Err(format!("docker compose up failed: exit {status}"));
    }
    Ok(())
}

pub fn compose_down() -> Result<(), String> {
    // Best-effort — uses container name from the compose file, not the file
    // itself, so we don't need AppHandle here.
    let _ = Command::new("docker")
        .args(["stop", "reggia-cc"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    Ok(())
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

#[tauri::command]
pub fn cmd_check_docker() -> DockerStatus {
    check_status()
}

#[tauri::command]
pub fn cmd_compose_up(app: AppHandle) -> Result<(), String> {
    let cfg = config::read_config(&app)?;
    compose_up(&app, &cfg)
}
