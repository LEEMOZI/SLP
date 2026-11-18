use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{
    Emitter, Manager,
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    RunEvent, WindowEvent,
};

/// 管理所有子进程（sidecar + workbench）
struct Services(Mutex<Vec<Child>>);

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

/// 判断是否为分发版（resource 目录中有打包的二进制）
fn is_bundled(resource_dir: &std::path::Path) -> bool {
    resource_dir.join("binaries/slp-workbench").exists()
}

/// 分发版：从 resource 目录启动 PyInstaller 打包的独立二进制
fn spawn_bundled_services(
    resource_dir: &std::path::Path,
) -> Result<Vec<Child>, String> {
    let binaries = resource_dir.join("binaries");
    let frontend_dist = resource_dir.join("frontend/dist");

    // 启动 sidecar
    let sidecar = binaries.join("slp-sidecar");
    if !sidecar.exists() {
        return Err(format!("sidecar binary not found: {:?}", sidecar));
    }
    let sidecar_child = Command::new(&sidecar)
        .env("SLP_CAPABILITY_ADAPTER_TOKEN", "local-internal-token")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("failed to spawn sidecar: {}", e))?;

    // 等待 sidecar 就绪
    let sidecar_ready = wait_port_ready("127.0.0.1:8790", Duration::from_secs(10));
    if !sidecar_ready {
        eprintln!("[SLP] Warning: sidecar not ready after 10s");
    }

    // 启动 workbench
    let workbench = binaries.join("slp-workbench");
    if !workbench.exists() {
        return Err(format!("workbench binary not found: {:?}", workbench));
    }
    let workbench_child = Command::new(&workbench)
        .env("SLP_FRONTEND_DIST", &frontend_dist)
        .env("SLP_CAPABILITY_ADAPTER_TOKEN", "local-internal-token")
        .env("SLP_CAPABILITY_ADAPTER_URL", "http://127.0.0.1:8790")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("failed to spawn workbench: {}", e))?;

    Ok(vec![sidecar_child, workbench_child])
}

/// 开发版：通过 start-services.sh 启动
fn spawn_dev_services() -> Result<Vec<Child>, String> {
    let project_root = std::env::var("SLP_PROJECT_ROOT")
        .map(PathBuf::from)
        .or_else(|_| {
            std::env::var("HOME").map(|h| PathBuf::from(h).join("Documents/1-SLP"))
        })
        .map_err(|e| format!("cannot determine project root: {}", e))?;

    let script = project_root.join("apps/workbench/scripts/start-services.sh");
    if !script.exists() {
        return Err(format!("start-services.sh not found at {:?}", script));
    }

    let child = Command::new("bash")
        .arg(&script)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("failed to spawn start-services.sh: {}", e))?;

    Ok(vec![child])
}

fn wait_port_ready(addr: &str, timeout: Duration) -> bool {
    let deadline = std::time::Instant::now() + timeout;
    while std::time::Instant::now() < deadline {
        if TcpStream::connect(addr).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    false
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(Services(Mutex::new(Vec::new())))
        .setup(|app| {
            let app_handle = app.handle().clone();

            // 启动后端服务
            let resource_dir = app.path().resource_dir().unwrap_or_default();
            let services = if is_bundled(&resource_dir) {
                match spawn_bundled_services(&resource_dir) {
                    Ok(children) => children,
                    Err(e) => {
                        eprintln!("[SLP] Bundled services failed: {}, falling back to dev mode", e);
                        spawn_dev_services().unwrap_or_default()
                    }
                }
            } else {
                match spawn_dev_services() {
                    Ok(children) => children,
                    Err(e) => {
                        eprintln!("[SLP] Failed to start services: {}", e);
                        vec![]
                    }
                }
            };
            *app.state::<Services>().0.lock().unwrap() = services;

            // 独立线程等待后端就绪：重新加载页面后再显示窗口
            // （webview 创建即开始导航，此时后端往往未就绪，首次加载会失败且不会自动重试）
            std::thread::spawn(move || {
                let ready = wait_backend_ready(Duration::from_secs(60));
                if let Some(window) = app_handle.get_webview_window("main") {
                    if ready {
                        // 端口就绪后稍作停顿，等 uvicorn 完成初始化，再重新导航
                        std::thread::sleep(Duration::from_millis(500));
                        let _ = window.eval("location.reload()");
                        let _ = window.show();
                        let _ = window.set_focus();
                    } else {
                        eprintln!("[SLP] Backend not ready after 60s, showing window anyway");
                        let _ = window.eval("location.reload()");
                        let _ = window.show();
                        let _ = window.set_focus();
                    }
                }
            });

            // 系统托盘
            let show_item = MenuItem::with_id(app, "show", "显示窗口", true, None::<String>)?;
            let check_update_item = MenuItem::with_id(app, "check-update", "检查更新", true, None::<String>)?;
            let quit_item = MenuItem::with_id(app, "quit", "退出 SLP 法律工作台", true, None::<String>)?;
            let menu = Menu::with_items(app, &[&show_item, &check_update_item, &quit_item])?;

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
                    "check-update" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.emit("slp-check-update", ());
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
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        if let RunEvent::Exit = event {
            if let Some(state) = app_handle.try_state::<Services>() {
                for mut child in state.0.lock().unwrap().drain(..) {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        }
    });
}
