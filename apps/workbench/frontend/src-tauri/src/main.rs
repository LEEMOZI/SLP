#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::TcpStream;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{
    Manager,
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    RunEvent, WindowEvent,
};

struct Backend(Mutex<Option<Child>>);

fn project_root() -> std::path::PathBuf {
    if let Ok(p) = std::env::var("SLP_PROJECT_ROOT") {
        return std::path::PathBuf::from(p);
    }
    let home = std::env::var("HOME").unwrap_or_default();
    std::path::PathBuf::from(home).join("Documents/1-SLP")
}

fn wait_backend_ready(timeout: Duration) -> bool {
    let deadline = std::time::Instant::now() + timeout;
    while std::time::Instant::now() < deadline {
        if TcpStream::connect("127.0.0.1:8780").is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    false
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .manage(Backend(Mutex::new(None)))
        .setup(|app| {
            let app_handle = app.handle().clone();

            // 启动后端
            let script = project_root().join("apps/workbench/scripts/start-services.sh");
            if script.exists() {
                if let Ok(child) = Command::new("bash")
                    .arg(&script)
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .spawn()
                {
                    *app.state::<Backend>().0.lock().unwrap() = Some(child);
                } else {
                    eprintln!("[SLP] Failed to spawn start-services.sh");
                }
            } else {
                eprintln!("[SLP] start-services.sh not found at {:?}", script);
            }

            // 独立线程等待后端就绪后显示窗口
            std::thread::spawn(move || {
                let ready = wait_backend_ready(Duration::from_secs(30));
                if let Some(window) = app_handle.get_webview_window("main") {
                    let _ = window.show();
                    let _ = window.set_focus();
                    if !ready {
                        eprintln!("[SLP] Backend not ready after 30s, showing window anyway");
                    }
                }
            });

            // 系统托盘
            let show_item = MenuItem::with_id(app, "show", "显示窗口", true, None::<String>)?;
            let quit_item = MenuItem::with_id(app, "quit", "退出 SLP 法律工作台", true, None::<String>)?;
            let menu = Menu::with_items(app, &[&show_item, &quit_item])?;

            TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().unwrap().clone())
                .icon_as_template(true)
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "show" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.unminimize();
                            let _ = w.set_focus();
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        if let Some(w) = tray.app_handle().get_webview_window("main") {
                            if w.is_visible().unwrap_or(false) {
                                let _ = w.hide();
                            } else {
                                let _ = w.show();
                                let _ = w.set_focus();
                            }
                        }
                    }
                })
                .build(app)?;

            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!(), |app_handle, event| {
            if let RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<Backend>() {
                    if let Some(mut child) = state.0.lock().unwrap().take() {
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                }
            }
        })
        .expect("error while running tauri application");
}
