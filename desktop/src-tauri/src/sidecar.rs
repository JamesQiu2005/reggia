//! Spawn the bundled `reggia-backend` PyInstaller sidecar with all env vars set,
//! and poll for readiness.

use std::time::{Duration, Instant};

use tauri::api::process::{Command, CommandChild, CommandEvent};
use tauri::{AppHandle, Manager};

use crate::config;
use crate::AppState;

pub struct BackendHandle(pub CommandChild);

impl BackendHandle {
    pub fn kill(self) -> Result<(), String> {
        self.0.kill().map_err(|e| e.to_string())
    }
}

pub fn spawn(app: &AppHandle, cfg: &config::UserConfig) -> Result<BackendHandle, String> {
    let workspace = config::workspace_path(app);
    let data_dir = config::config_dir(app);
    let envs = vec![
        (
            "REGGIA_DATA_DIR".into(),
            data_dir.to_string_lossy().into_owned(),
        ),
        ("NOTION_API_KEY".into(), cfg.notion_api_key.clone()),
        ("NOTION_PAGE_WORK".into(), cfg.notion_page_ids.work.clone()),
        (
            "NOTION_PAGE_RESEARCH".into(),
            cfg.notion_page_ids.research.clone(),
        ),
        (
            "NOTION_PAGE_INTELLECTUAL".into(),
            cfg.notion_page_ids.intellectual.clone(),
        ),
        (
            "NOTION_PAGE_PERSONAL".into(),
            cfg.notion_page_ids.personal.clone(),
        ),
        (
            "NOTION_PAGE_INDEX".into(),
            cfg.notion_page_ids.index.clone(),
        ),
        ("DEEPSEEK_API_KEY".into(), cfg.deepseek_api_key.clone()),
        ("CC_MODE".into(), "docker".into()),
        (
            "REGGIA_WORKSPACE".into(),
            workspace.to_string_lossy().into_owned(),
        ),
    ]
    .into_iter()
    .collect::<std::collections::HashMap<String, String>>();

    let exe = app
        .path_resolver()
        .resolve_resource("binaries/reggia-backend/reggia-backend")
        .ok_or("could not locate bundled reggia-backend binary")?;
    let (mut rx, child) = Command::new(exe.to_string_lossy().to_string())
        .envs(envs)
        .spawn()
        .map_err(|e| format!("spawn sidecar: {e}"))?;

    // Drain stdout/stderr so the OS pipe buffer never fills up. We don't try to
    // surface logs to the UI yet; they're printed to the host terminal in dev,
    // and silently dropped in release.
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) | CommandEvent::Stderr(line) => {
                    eprintln!("[backend] {line}");
                }
                CommandEvent::Error(err) => eprintln!("[backend error] {err}"),
                CommandEvent::Terminated(payload) => {
                    eprintln!("[backend terminated] {:?}", payload);
                    break;
                }
                _ => {}
            }
        }
    });

    Ok(BackendHandle(child))
}

/// Poll `/chat/config` until 200, with a 15s budget.
pub async fn wait_ready() -> Result<(), String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_millis(800))
        .build()
        .map_err(|e| e.to_string())?;
    let deadline = Instant::now() + Duration::from_secs(15);
    while Instant::now() < deadline {
        if let Ok(resp) = client
            .get("http://127.0.0.1:8000/chat/config")
            .send()
            .await
        {
            if resp.status().is_success() {
                return Ok(());
            }
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    Err("backend did not become ready within 15s".into())
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

/// Read config, start docker compose, spawn the backend, wait for it to be
/// ready, then navigate the main window to http://127.0.0.1:8000.
#[tauri::command]
pub async fn cmd_start_backend(app: AppHandle) -> Result<(), String> {
    let cfg = config::read_config(&app)?;
    // Ensure workspace dir + Docker container before spawning the backend.
    config::seed_workspace(&app)?;
    crate::docker::compose_up(&app, &cfg)?;
    let handle = spawn(&app, &cfg)?;
    {
        let state: tauri::State<AppState> = app.state();
        let mut guard = state.backend.lock().map_err(|e| e.to_string())?;
        if let Some(prev) = guard.take() {
            let _ = prev.kill();
        }
        *guard = Some(handle);
    }
    wait_ready().await?;
    if let Some(win) = app.get_window("main") {
        win.eval("window.location.replace('http://127.0.0.1:8000')")
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
pub async fn cmd_backend_ready() -> Result<bool, String> {
    Ok(wait_ready().await.is_ok())
}
