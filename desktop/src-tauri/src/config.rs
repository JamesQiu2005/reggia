//! User config: API keys + Notion page IDs.
//!
//! Stored at the platform's standard config location:
//!   macOS:   ~/Library/Application Support/com.reggia.desktop/config.json
//!   Windows: %APPDATA%\com.reggia.desktop\config.json
//!
//! `chat_workspace/` is seeded alongside, since the Docker volume mount
//! points at it.

use std::fs;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use tauri::AppHandle;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct NotionPageIds {
    pub work: String,
    pub research: String,
    pub intellectual: String,
    pub personal: String,
    pub index: String,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct UserConfig {
    pub version: u32,
    pub deepseek_api_key: String,
    pub notion_api_key: String,
    pub notion_page_ids: NotionPageIds,
}

pub fn config_dir(app: &AppHandle) -> PathBuf {
    app.path_resolver()
        .app_config_dir()
        .expect("could not resolve app config dir")
}

pub fn config_path(app: &AppHandle) -> PathBuf {
    config_dir(app).join("config.json")
}

pub fn workspace_path(app: &AppHandle) -> PathBuf {
    config_dir(app).join("chat_workspace")
}

pub fn read_config(app: &AppHandle) -> Result<UserConfig, String> {
    let path = config_path(app);
    let bytes = fs::read(&path).map_err(|e| format!("read {}: {}", path.display(), e))?;
    serde_json::from_slice(&bytes).map_err(|e| format!("parse config: {e}"))
}

pub fn write_config(app: &AppHandle, cfg: &UserConfig) -> Result<(), String> {
    let dir = config_dir(app);
    fs::create_dir_all(&dir).map_err(|e| format!("mkdir {}: {}", dir.display(), e))?;
    let path = config_path(app);
    let json = serde_json::to_vec_pretty(cfg).map_err(|e| e.to_string())?;
    fs::write(&path, json).map_err(|e| format!("write {}: {}", path.display(), e))?;
    Ok(())
}

pub fn delete_config() -> Result<(), String> {
    // Called from the system tray, where we don't have AppHandle in scope.
    // Resolve config dir the long way via `dirs`.
    let base = dirs::config_dir().ok_or("no config dir")?;
    let path = base.join("com.reggia.desktop").join("config.json");
    if path.exists() {
        fs::remove_file(&path).map_err(|e| format!("remove {}: {}", path.display(), e))?;
    }
    Ok(())
}

/// Copy bundled `resources/chat_workspace_seed/` into the user's config dir.
/// Idempotent — only seeds if the destination is missing.
pub fn seed_workspace(app: &AppHandle) -> Result<PathBuf, String> {
    let dst = workspace_path(app);
    if dst.exists() {
        return Ok(dst);
    }
    let src = app
        .path_resolver()
        .resolve_resource("resources/chat_workspace_seed")
        .ok_or("could not locate chat_workspace_seed in bundled resources")?;
    copy_dir_recursive(&src, &dst).map_err(|e| format!("seed workspace: {e}"))?;
    Ok(dst)
}

fn copy_dir_recursive(src: &std::path::Path, dst: &std::path::Path) -> std::io::Result<()> {
    fs::create_dir_all(dst)?;
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let from = entry.path();
        let to = dst.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_dir_recursive(&from, &to)?;
        } else {
            fs::copy(&from, &to)?;
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

#[tauri::command]
pub fn cmd_get_config(app: AppHandle) -> Option<UserConfig> {
    read_config(&app).ok()
}

#[tauri::command]
pub fn cmd_save_config(app: AppHandle, cfg: UserConfig) -> Result<(), String> {
    write_config(&app, &cfg)?;
    seed_workspace(&app)?;
    Ok(())
}

#[tauri::command]
pub fn cmd_delete_config(app: AppHandle) -> Result<(), String> {
    let path = config_path(&app);
    if path.exists() {
        fs::remove_file(&path).map_err(|e| format!("remove {}: {}", path.display(), e))?;
    }
    Ok(())
}
