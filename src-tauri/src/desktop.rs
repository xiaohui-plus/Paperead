// 桌面端：本机 Python sidecar + 研究 agent + 自动更新。
// 这些模块只在桌面平台编译，安卓端不会带上。

#[path = "agent_sidecar.rs"]
mod agent_sidecar;
#[path = "api.rs"]
mod api;
#[path = "paths.rs"]
mod paths;
#[path = "sidecar.rs"]
mod sidecar;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use tauri::menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_updater::UpdaterExt;
use url::Url;

use agent_sidecar::AgentSidecar;
use sidecar::Sidecar;

static TRAY_READY: AtomicBool = AtomicBool::new(false);

struct AppState {
    port: u16,
    token: String,
    sidecar: Mutex<Option<Sidecar>>,
    agent: Mutex<Option<AgentSidecar>>,
}

pub fn run_desktop() {
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
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![install_update])
        .on_window_event(|window, event| {
            if window.label() != "main" {
                return;
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                // 研究/综述任务在 sidecar 里跑；有托盘时关窗只收起界面，别杀掉任务。
                if !TRAY_READY.load(Ordering::Relaxed) {
                    return;
                }
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .setup(|app| {
            let handle = app.handle().clone();
            let data_dir = paths::ensure_data_dir(&handle);
            // Reserve the research-agent endpoint before Python starts so the
            // proxy can be configured by environment instead of a file.
            let agent_endpoint = agent_sidecar::free_port()
                .map(|port| (port, agent_sidecar::random_token()))
                .map_err(|error| {
                    paths::log_line(&handle, &format!("reserve agent port failed: {error}"));
                })
                .ok();
            let sidecar = match Sidecar::start(&handle, agent_endpoint.clone()) {
                Ok(sidecar) => sidecar,
                Err(error) => {
                    paths::log_line(&handle, &format!("start failed: {error}"));
                    handle
                        .dialog()
                        .message(&error)
                        .title("Paperead 启动失败")
                        .kind(MessageDialogKind::Error)
                        .blocking_show();
                    return Err(Box::new(std::io::Error::other(error)));
                }
            };

            let port = sidecar.port;
            let token = sidecar.token.clone();

            let agent = match agent_endpoint {
                Some((agent_port, agent_token)) => {
                    match AgentSidecar::start(&handle, port, &token, &data_dir, agent_port, agent_token) {
                        Ok(agent) => Some(agent),
                        Err(error) => {
                            // Research is optional: the library and reader must still open.
                            paths::log_line(&handle, &format!("agent start failed: {error}"));
                            None
                        }
                    }
                }
                None => None,
            };

            app.manage(AppState {
                port,
                token,
                sidecar: Mutex::new(Some(sidecar)),
                agent: Mutex::new(agent),
            });

            paths::log_line(&handle, "creating main window");
            if let Err(error) = create_main_window(app, port, &data_dir) {
                paths::log_line(&handle, &format!("create window failed: {error}"));
                return Err(Box::new(error));
            }
            paths::log_line(&handle, "setting up menu");
            if let Err(error) = setup_menu(app) {
                paths::log_line(&handle, &format!("setup menu failed: {error}"));
                return Err(Box::new(error));
            }
            if let Err(error) = setup_tray(app) {
                // 托盘失败时不拦截关窗，否则窗口藏起来就再也找不回来。
                paths::log_line(&handle, &format!("setup tray failed: {error}"));
            } else {
                TRAY_READY.store(true, Ordering::Relaxed);
            }

            paths::log_line(&handle, "desktop ready");
            maybe_onboard(&handle, port, &data_dir);

            let updater_app = handle.clone();
            tauri::async_runtime::spawn(async move {
                let interval = paths::var("PAPEREAD_UPDATE_INTERVAL_SECS")
                    .and_then(|value| value.parse::<u64>().ok())
                    .unwrap_or(6 * 60 * 60)
                    .max(30);
                paths::log_line(
                    &updater_app,
                    &format!("auto update check enabled, interval={interval}s"),
                );
                tokio::time::sleep(std::time::Duration::from_secs(8)).await;
                loop {
                    check_updates(updater_app.clone(), false).await;
                    tokio::time::sleep(std::time::Duration::from_secs(interval)).await;
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build Paperead desktop")
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
        .title("Paperead")
        .inner_size(1280.0, 860.0)
        .min_inner_size(1000.0, 680.0)
        .data_directory(webview_dir)
        // 外部文件拖放必须由 Tauri 接管：WebView2 收不到 HTML5 drop，
        // 前端通过 onDragDropEvent 拿本地路径，再调 /api/papers/import 入库。
        .drag_and_drop(true)
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

    let about = MenuItemBuilder::with_id("about", "关于 Paperead").build(app)?;
    let check_update = MenuItemBuilder::with_id("check_update", "检查更新").build(app)?;
    let help = SubmenuBuilder::new(app, "帮助")
        .item(&check_update)
        .item(&about)
        .build()?;
    let menu = MenuBuilder::new(app).items(&[&file, &help]).build()?;

    app.set_menu(menu)?;
    app.on_menu_event(|app, event| match event.id().as_ref() {
        "open" => pick_pdf(app.clone()),
        "print" => {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.eval("window.print()");
            }
        }
        "check_update" => {
            tauri::async_runtime::spawn(check_updates(app.clone(), true));
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

fn setup_tray(app: &mut tauri::App) -> tauri::Result<()> {
    let show = MenuItemBuilder::with_id("tray_show", "显示主窗口").build(app)?;
    let quit = MenuItemBuilder::with_id("tray_quit", "退出").build(app)?;
    let menu = MenuBuilder::new(app).items(&[&show, &quit]).build()?;

    let mut builder = TrayIconBuilder::with_id("main-tray")
        .menu(&menu)
        .tooltip("Paperead · 研究与综述任务在后台继续运行")
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "tray_show" => focus_main(app),
            "tray_quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                focus_main(tray.app_handle());
            }
        });

    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    Ok(())
}

fn maybe_onboard(app: &AppHandle, port: u16, data_dir: &std::path::Path) {
    let flag = data_dir.join("onboarded");
    if flag.exists() {
        return;
    }
    let _ = std::fs::write(&flag, "1");

    let target = format!("http://127.0.0.1:{port}/?settings=1");
    let window = app.get_webview_window("main");
    app.dialog()
        .message(
            "Paperead 已安装。\n\nPDF 和阅读数据只保存在本机；翻译/导读时，论文文本会发送给你配置的模型服务商。\n\n接下来请在设置里填写 API Key。",
        )
        .title("欢迎使用 Paperead")
        .kind(MessageDialogKind::Info)
        .show(move |_| {
            if let Some(window) = window {
                if let Ok(url) = Url::parse(&target) {
                    let _ = window.navigate(url);
                }
            }
        });
}

fn updater_for(app: &AppHandle) -> Result<tauri_plugin_updater::Updater, String> {
    let endpoint = paths::var("PAPEREAD_UPDATE_ENDPOINT")
        .and_then(|value| Url::parse(&value).ok());
    if paths::var("PAPEREAD_UPDATE_ENDPOINT").is_some() && endpoint.is_none() {
        paths::log_line(app, "invalid PAPEREAD_UPDATE_ENDPOINT, using config endpoint");
    }

    let builder = if let Some(url) = endpoint {
        match app.updater_builder().endpoints(vec![url]) {
            Ok(builder) => builder,
            Err(error) => {
                paths::log_line(app, &format!("invalid update endpoint: {error}"));
                app.updater_builder()
            }
        }
    } else {
        app.updater_builder()
    };

    builder
        .build()
        .map_err(|error| format!("更新检查不可用：{error}"))
}

/// 设置页「立即更新」：检查 → 下载 → 交给安装器，随后应用退出。
#[tauri::command]
async fn install_update(app: AppHandle) -> Result<String, String> {
    let updater = updater_for(&app)?;
    match updater.check().await {
        Ok(Some(update)) => {
            let version = update.version.clone();
            paths::log_line(&app, &format!("manual update download started: {version}"));
            let bytes = update
                .download(|_, _| {}, || {})
                .await
                .map_err(|error| format!("下载更新失败：{error}"))?;
            install_downloaded_update(&app, &version, &bytes)?;
            Ok(version)
        }
        Ok(None) => Err("当前已是最新版本".into()),
        Err(error) => Err(format!("检查更新失败：{error}")),
    }
}

async fn check_updates(app: AppHandle, interactive: bool) {
    let updater = match updater_for(&app) {
        Ok(updater) => updater,
        Err(error) => {
            paths::log_line(&app, &format!("updater init failed: {error}"));
            if interactive {
                app.dialog()
                    .message(format!("更新检查不可用：{error}"))
                    .title("检查更新")
                    .kind(MessageDialogKind::Error)
                    .show(|_| {});
            }
            return;
        }
    };

    match updater.check().await {
        Ok(Some(update)) => {
            let version = update.version.clone();
            let app_for_dialog = app.clone();
            app.dialog()
                .message(format!("发现新版本 {version}，是否下载并安装？"))
                .title("发现更新")
                .buttons(MessageDialogButtons::OkCancel)
                .show(move |confirmed| {
                    if !confirmed {
                        return;
                    }
                    let app_for_update = app_for_dialog.clone();
                    let version_for_update = version.clone();
                    tauri::async_runtime::spawn(async move {
                        match update.download(|_, _| {}, || {}).await {
                            Ok(bytes) => {
                                if let Err(error) = install_downloaded_update(
                                    &app_for_update,
                                    &version_for_update,
                                    &bytes,
                                ) {
                                    app_for_update
                                        .dialog()
                                        .message(format!("更新失败：{error}"))
                                        .title("更新失败")
                                        .kind(MessageDialogKind::Error)
                                        .show(|_| {});
                                }
                            }
                            Err(error) => {
                                app_for_update
                                    .dialog()
                                    .message(format!("更新失败：{error}"))
                                    .title("更新失败")
                                    .kind(MessageDialogKind::Error)
                                    .show(|_| {});
                            }
                        }
                    });
                });
        }
        Ok(None) => {
            if interactive {
                app.dialog()
                    .message("当前已是最新版本。")
                    .title("检查更新")
                    .kind(MessageDialogKind::Info)
                    .show(|_| {});
            }
        }
        Err(error) => {
            paths::log_line(&app, &format!("update check failed: {error}"));
            if interactive {
                app.dialog()
                    .message(format!("检查更新失败：{error}"))
                    .title("检查更新")
                    .kind(MessageDialogKind::Error)
                    .show(|_| {});
            }
        }
    }
}

#[cfg(windows)]
fn install_downloaded_update(
    app: &AppHandle,
    version: &str,
    bytes: &[u8],
) -> Result<(), String> {
    use std::os::windows::process::CommandExt;

    let temp_dir = paths::ensure_data_dir(app).join("update-temp");
    std::fs::create_dir_all(&temp_dir).map_err(|error| format!("创建更新目录失败：{error}"))?;
    let installer = temp_dir.join(format!("paperead-{version}-setup.exe"));
    std::fs::write(&installer, bytes).map_err(|error| format!("写入更新包失败：{error}"))?;

    let cmd = std::env::var("SystemRoot")
        .map(|root| format!("{root}\\System32\\cmd.exe"))
        .unwrap_or_else(|_| "cmd.exe".into());
    // 用批处理延迟启动 NSIS：避免 exe 仍被占用，也避免 Rust 参数转义破坏 start 的引号。
    let batch = temp_dir.join("run-update.bat");
    let batch_content = format!(
        "@echo off\r\nset \"TEMP={}\"\r\nset \"TMP={}\"\r\nping 127.0.0.1 -n 12 >nul\r\nstart \"\" \"{}\" /P /UPDATE /R\r\n",
        temp_dir.display(),
        temp_dir.display(),
        installer.display()
    );
    std::fs::write(&batch, batch_content).map_err(|error| format!("写入更新脚本失败：{error}"))?;
    std::process::Command::new(cmd)
        .env("TEMP", &temp_dir)
        .env("TMP", &temp_dir)
        .args(["/C", &batch.to_string_lossy()])
        .creation_flags(0x0800_0000)
        .spawn()
        .map_err(|error| format!("启动更新安装器失败：{error}"))?;

    paths::log_line(
        app,
        &format!("update installer scheduled: {}", installer.display()),
    );
    app.exit(0);
    Ok(())
}

#[cfg(not(windows))]
fn install_downloaded_update(
    _app: &AppHandle,
    _version: &str,
    _bytes: &[u8],
) -> Result<(), String> {
    Err("当前平台暂不支持自动安装更新".into())
}

fn show_about(app: &AppHandle) {    let message = format!(
        "Paperead {}\n\nAGPL-3.0 开源\n源码与安装包见 GitHub Releases。",
        env!("CARGO_PKG_VERSION")
    );
    app.dialog()
        .message(message)
        .title("关于 Paperead")
        .show(|_| {});
}

fn shutdown(app: &AppHandle) {
    if let Some(state) = app.try_state::<AppState>() {
        if let Ok(mut guard) = state.agent.lock() {
            if let Some(mut agent) = guard.take() {
                agent.stop();
            }
        }
        if let Ok(mut guard) = state.sidecar.lock() {
            if let Some(mut sidecar) = guard.take() {
                sidecar.stop();
            }
        }
    }
}

