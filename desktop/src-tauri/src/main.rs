// Reggia desktop wrapper — Tauri entry point.
//
// Lifecycle:
//   1. Read user config.json (~/Library/Application Support/Reggia/config.json on Mac,
//      %APPDATA%\Reggia\config.json on Windows).
//   2. If missing -> load wizard/index.html in the main window.
//   3. If present -> check Docker, spawn `docker compose up -d` with the bundled
//      compose file, spawn the bundled reggia-backend sidecar with env vars,
//      wait for the backend, then navigate the window to http://127.0.0.1:8000.
//   4. On window close: SIGTERM the sidecar; leave docker compose running.
//   5. Tray menu: "Show Window", "Stop Reggia services" (docker compose down),
//      "Reset configuration" (wipes config.json and reloads wizard), "Quit".

#![cfg_attr(
    all(not(debug_assertions), target_os = "windows"),
    windows_subsystem = "windows"
)]

mod config;
mod docker;
mod notion_setup;
mod sidecar;

use std::sync::Mutex;

use tauri::{
    CustomMenuItem, Manager, RunEvent, SystemTray, SystemTrayEvent, SystemTrayMenu,
    SystemTrayMenuItem, WindowEvent,
};

use sidecar::BackendHandle;

/// Shared handle to the running backend sidecar.
pub struct AppState {
    pub backend: Mutex<Option<BackendHandle>>,
}

fn build_tray() -> SystemTray {
    let menu = SystemTrayMenu::new()
        .add_item(CustomMenuItem::new("show", "Show Window"))
        .add_native_item(SystemTrayMenuItem::Separator)
        .add_item(CustomMenuItem::new("stop_services", "Stop Reggia services"))
        .add_item(CustomMenuItem::new("reset_config", "Reset configuration"))
        .add_native_item(SystemTrayMenuItem::Separator)
        .add_item(CustomMenuItem::new("quit", "Quit"));
    SystemTray::new().with_menu(menu)
}

fn main() {
    let app = tauri::Builder::default()
        .manage(AppState {
            backend: Mutex::new(None),
        })
        .system_tray(build_tray())
        .on_system_tray_event(|app, event| {
            if let SystemTrayEvent::MenuItemClick { id, .. } = event {
                match id.as_str() {
                    "show" => {
                        if let Some(win) = app.get_window("main") {
                            let _ = win.show();
                            let _ = win.set_focus();
                        }
                    }
                    "stop_services" => {
                        let _ = docker::compose_down();
                    }
                    "reset_config" => {
                        if let Err(e) = config::delete_config() {
                            eprintln!("reset_config: {e:?}");
                        }
                        if let Some(win) = app.get_window("main") {
                            let _ = win.eval("window.location.reload()");
                        }
                    }
                    "quit" => {
                        // RunEvent::ExitRequested will fire and clean up.
                        std::process::exit(0);
                    }
                    _ => {}
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            config::cmd_get_config,
            config::cmd_save_config,
            config::cmd_delete_config,
            docker::cmd_check_docker,
            docker::cmd_compose_up,
            notion_setup::cmd_validate_deepseek_key,
            notion_setup::cmd_validate_notion_token,
            notion_setup::cmd_validate_parent_page,
            notion_setup::cmd_create_notion_pages,
            sidecar::cmd_start_backend,
            sidecar::cmd_backend_ready,
        ])
        .setup(|app| {
            // On startup, if a config exists, navigate the main window straight
            // to the backend URL once it's responsive. Otherwise, the default
            // wizard/index.html stays loaded.
            let handle = app.handle();
            tauri::async_runtime::spawn(async move {
                if config::read_config(&handle).is_ok() {
                    // Boot the services; the JS in wizard/index.html will
                    // detect the existing config and call start_backend itself.
                    // Nothing to do here.
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        if let RunEvent::ExitRequested { .. } = event {
            let state: tauri::State<AppState> = app_handle.state();
            let lock_result = state.backend.lock();
            if let Ok(mut guard) = lock_result {
                if let Some(handle) = guard.take() {
                    let _ = handle.kill();
                }
            }
        }
    });
}
