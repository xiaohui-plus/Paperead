#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod api;
mod paths;
mod sidecar;

use std::sync::Mutex;

use tauri::menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder};
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use url::Url;

use sidecar::Sidecar;

struct AppState {
    port: u16,
    token: String,
    sidecar: Mutex<Option<Sidecar>>,
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            focus_main(app);
            if let Some(path) = argv
                .iter()
                .skip(1)
                .find(|arg| arg.to_ascii_lowercase().ends_with(".pdf"))
            {
                open_path(app.clone(), path.clone());
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let handle = app.handle().clone();
            let sidecar = match Sidecar::start(&handle) {
                Ok(sidecar) => sidecar,
                Err(error) => {
                    paths::log_line(&handle, &format!("start failed: {error}"));
                    handle
                        .dialog()
                        .message(&error)
                        .title("paper-reflow 启动失败")
                        .kind(MessageDialogKind::Error)
                        .blocking_show();
                    return Err(Box::new(std::io::Error::other(error)));
                }
            };

            let port = sidecar.port;
            app.manage(AppState {
                port,
                token: sidecar.token.clone(),
                sidecar: Mutex::new(Some(sidecar)),
            });

            paths::log_line(&handle, "creating main window");
            let data_dir = paths::ensure_data_dir(&handle);
            if let Err(error) = create_main_window(app, port, &data_dir) {
                paths::log_line(&handle, &format!("create window failed: {error}"));
                return Err(Box::new(error));
            }
            paths::log_line(&handle, "setting up menu");
            if let Err(error) = setup_menu(app) {
                paths::log_line(&handle, &format!("setup menu failed: {error}"));
                return Err(Box::new(error));
            }

            paths::log_line(&handle, "desktop ready");
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build paper-reflow desktop")
        .run(|app_handle, event| {
            if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
                shutdown(app_handle);
            }
        });
}

fn create_main_window(app: &mut tauri::App, port: u16, data_dir: &std::path::Path) -> tauri::Result<()> {
    let target = format!("http://127.0.0.1:{port}/");
    let url = Url::parse(&target).expect("valid local url");
    let allowed = format!("http://127.0.0.1:{port}");
    let webview_dir = data_dir.join("webview2");
    let _ = std::fs::create_dir_all(&webview_dir);

    let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
        .title("paper-reflow")
        .inner_size(1280.0, 860.0)
        .min_inner_size(1000.0, 680.0)
        .data_directory(webview_dir)
        .drag_and_drop(false)
        .center()
        .on_navigation(move |url| {
            if url.as_str().starts_with(&allowed) {
                return true;
            }
            if url.scheme() == "http" || url.scheme() == "https" {
                let _ = tauri_plugin_opener::open_url(url.as_str(), None::<&str>);
            }
            false
        })
        .build()?;

    window.show()?;
    window.set_focus()?;
    Ok(())
}

fn setup_menu(app: &mut tauri::App) -> tauri::Result<()> {
    let open = MenuItemBuilder::with_id("open", "打开 PDF...")
        .accelerator("CmdOrCtrl+O")
        .build(app)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let print = MenuItemBuilder::with_id("print", "打印")
        .accelerator("CmdOrCtrl+P")
        .build(app)?;
    let quit = MenuItemBuilder::with_id("quit", "退出")
        .accelerator("CmdOrCtrl+Q")
        .build(app)?;
    let file = SubmenuBuilder::new(app, "文件")
        .item(&open)
        .item(&separator)
        .item(&print)
        .item(&quit)
        .build()?;

    let about = MenuItemBuilder::with_id("about", "关于 paper-reflow").build(app)?;
    let help = SubmenuBuilder::new(app, "帮助").item(&about).build()?;
    let menu = MenuBuilder::new(app).items(&[&file, &help]).build()?;

    app.set_menu(menu)?;
    app.on_menu_event(|app, event| match event.id().as_ref() {
        "open" => pick_pdf(app.clone()),
        "print" => {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.eval("window.print()");
            }
        }
        "quit" => app.exit(0),
        "about" => show_about(app),
        _ => {}
    });
    Ok(())
}

fn pick_pdf(app: AppHandle) {
    app.dialog()
        .file()
        .add_filter("PDF", &["pdf"])
        .pick_file(move |file| {
            if let Some(path) = file.and_then(|file| file.into_path().ok()) {
                open_path(app.clone(), path.to_string_lossy().to_string());
            }
        });
}

fn open_path(app: AppHandle, path: String) {
    let (port, token) = {
        let state = app.state::<AppState>();
        (state.port, state.token.clone())
    };

    std::thread::spawn(move || match api::import_pdf(port, &token, &path) {
        Ok(paper_id) => {
            if let Some(window) = app.get_webview_window("main") {
                let target = format!("http://127.0.0.1:{port}/?p={paper_id}");
                if let Ok(url) = Url::parse(&target) {
                    let _ = window.navigate(url);
                }
                let _ = window.set_focus();
            }
        }
        Err(error) => {
            app.dialog()
                .message(&error)
                .title("导入 PDF 失败")
                .kind(MessageDialogKind::Error)
                .show(|_| {});
        }
    });
}

fn focus_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn show_about(app: &AppHandle) {
    let message = format!(
        "paper-reflow {}\n\nAGPL-3.0 开源\n源码与安装包见 GitHub Releases。",
        env!("CARGO_PKG_VERSION")
    );
    app.dialog()
        .message(message)
        .title("关于 paper-reflow")
        .show(|_| {});
}

fn shutdown(app: &AppHandle) {
    if let Some(state) = app.try_state::<AppState>() {
        if let Ok(mut guard) = state.sidecar.lock() {
            if let Some(mut sidecar) = guard.take() {
                sidecar.stop();
            }
        }
    }
}
