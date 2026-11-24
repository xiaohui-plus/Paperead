// 桌面端：本机 Python sidecar + 研究 agent + 自动更新。
// 这些模块只在桌面平台编译，安卓端不会带上。

#[path = "agent_sidecar.rs"]
mod agent_sidecar;
#[path = "api.rs"]
mod api;
#[path = "browser_ext.rs"]
mod browser_ext;
#[path = "feedback.rs"]
mod feedback;
#[path = "paths.rs"]
mod paths;
#[path = "sidecar.rs"]
mod sidecar;

use std::sync::atomic::{AtomicBool, AtomicU16, Ordering};
use std::sync::{Arc, Mutex};

use tauri::menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_updater::UpdaterExt;
use url::Url;

use agent_sidecar::AgentSidecar;
use sidecar::Sidecar;

static TRAY_READY: AtomicBool = AtomicBool::new(false);
/// 应用开始退出后，后台启动线程不能再把 agent 放进状态（否则句柄没人回收）。
static SHUTTING_DOWN: AtomicBool = AtomicBool::new(false);

struct Backend {
    port: u16,
    token: String,
    sidecar: Sidecar,
}

struct AppState {
    /// sidecar 就绪后才填充：splash 窗口先显示，sidecar 在后台并行启动。
    backend: Mutex<Option<Backend>>,
    agent: Mutex<Option<AgentSidecar>>,
}

impl AppState {
    /// 取本机 sidecar 的端口与令牌；后台启动尚未就绪时返回可展示的提示。
    fn connection(&self) -> Result<(u16, String), String> {
        let guard = self
            .backend
            .lock()
            .map_err(|_| "本地服务状态异常，请重启应用".to_string())?;
        match guard.as_ref() {
            Some(backend) => Ok((backend.port, backend.token.clone())),
            None => Err("本地服务正在启动，请稍后再试".into()),
        }
    }
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
        .invoke_handler(tauri::generate_handler![
            install_update,
            install_prepared_update,
            get_update_status,
            check_desktop_update,
            restart_app,
            save_pdf_file,
            save_translated_pdf,
            browser_ext::browser_extension_setup,
            browser_ext::open_browser_extensions,
            browser_ext::open_extension_folder
        ])
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
            let boot_started = std::time::Instant::now();
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
            // 先立状态与更新中心，sidecar 就绪后再回填 backend。
            app.manage(AppState {
                backend: Mutex::new(None),
                agent: Mutex::new(None),
            });
            // 后台更新中心：先下载新版本，下载完只差一次点击即可安装。
            app.manage(UpdateCenter::default());

            // 首屏先出极简加载页：WebView2 初始化与 sidecar 启动并行。
            // 端口在启动时才确定，导航白名单只放行实际端口（先为 0 = 不放行回环）。
            let sidecar_port = Arc::new(AtomicU16::new(0));
            paths::log_line(&handle, "creating main window");
            if let Err(error) = create_main_window(app, &data_dir, sidecar_port.clone()) {
                paths::log_line(&handle, &format!("create window failed: {error}"));
                return Err(Box::new(error));
            }
            paths::log_line(
                &handle,
                &format!("splash shown in {}ms", boot_started.elapsed().as_millis()),
            );

            // sidecar 在后台启动，就绪后切到真实页面并拉起研究 agent。
            spawn_sidecar(
                handle.clone(),
                data_dir.clone(),
                agent_endpoint,
                sidecar_port,
            );

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

            paths::log_line(
                &handle,
                &format!("desktop ready in {}ms", boot_started.elapsed().as_millis()),
            );


            let updater_app = handle.clone();
            tauri::async_runtime::spawn(async move {
                let interval = paths::var("PAPEREAD_UPDATE_INTERVAL_SECS")
                    .and_then(|value| value.parse::<u64>().ok())
                    .unwrap_or(6 * 60 * 60)
                    .max(30);
                paths::log_line(
                    &updater_app,
                    &format!("auto update download enabled, interval={interval}s"),
                );
                tokio::time::sleep(std::time::Duration::from_secs(8)).await;
                loop {
                    // 启动后静默检查并把新版本下到本地：用户在侧栏看到更新图标时
                    // 安装包已经就绪，点击只差启动安装器这一步。
                    prepare_update_once(&updater_app).await;
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

/// 在后台线程启动研究 agent：窗口已经显示，不再等 node 冷启动与健康轮询结束。
///
/// agent 本身仍是 sidecar 之后启动（端口/token 由 sidecar 启动时注入），只是
/// 不再阻塞建窗。研究页在 agent 未就绪时返回 503，前端会退避重连/重试。
fn spawn_agent(
    app: AppHandle,
    python_port: u16,
    python_token: String,
    data_dir: std::path::PathBuf,
    agent_port: u16,
    agent_token: String,
) {
    std::thread::spawn(move || {
        let started = std::time::Instant::now();
        match AgentSidecar::start(
            &app,
            python_port,
            &python_token,
            &data_dir,
            agent_port,
            agent_token,
        ) {
            Ok(mut agent) => {
                if SHUTTING_DOWN.load(Ordering::Relaxed) {
                    agent.stop();
                    return;
                }
                // 用 Option 承接：没被状态接管时就还在这里，可以主动 stop。
                let mut pending = Some(agent);
                let mut stored = false;
                if let Some(state) = app.try_state::<AppState>() {
                    if let Ok(mut guard) = state.agent.lock() {
                        // 退出流程可能刚把状态置空并开始 stop：再确认一次。
                        if !SHUTTING_DOWN.load(Ordering::Relaxed) {
                            *guard = pending.take();
                            stored = true;
                        }
                    }
                }
                if let Some(mut leftover) = pending {
                    // 没接管句柄时必须主动结束，避免留下孤儿 node 进程。
                    leftover.stop();
                    return;
                }
                if stored {
                    paths::log_line(
                        &app,
                        &format!(
                            "agent ready in {}ms (background, window already shown)",
                            started.elapsed().as_millis()
                        ),
                    );
                }
            }
            Err(error) => {
                // Research is optional: the library and reader must still work.
                paths::log_line(
                    &app,
                    &format!(
                        "agent start failed after {}ms: {error}",
                        started.elapsed().as_millis()
                    ),
                );
            }
        }
    });
}

/// 首屏窗口：先加载内置极简加载页，sidecar 就绪后由 `spawn_sidecar` 切到真实页面。
fn create_main_window(
    app: &mut tauri::App,
    data_dir: &std::path::Path,
    sidecar_port: Arc<AtomicU16>,
) -> tauri::Result<()> {
    let webview_dir = data_dir.join("webview2");
    let _ = std::fs::create_dir_all(&webview_dir);

    let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("splash.html".into()))
        .title("Paperead")
        .inner_size(1280.0, 860.0)
        // 分屏贴边时要能继续拉窄：过高的最小宽度会让 Windows 分屏卡在 1000px 以上。
        .min_inner_size(640.0, 520.0)
        .data_directory(webview_dir)
        // 外部文件拖放必须由 Tauri 接管：WebView2 收不到 HTML5 drop，
        // 前端通过 onDragDropEvent 拿本地路径，再调 /api/papers/import 入库。
        .drag_and_drop(true)
        .center()
        .on_navigation(move |url| {
            // 加载页走 Tauri 资源协议；真实页面与 ?p= 跳转走本机 sidecar 实际端口。
            if is_local_app_url(url, sidecar_port.load(Ordering::Relaxed)) {
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

/// 是否属于应用自己的页面：Tauri 资源协议，或本机回环地址上当前 sidecar 端口。
fn is_local_app_url(url: &Url, port: u16) -> bool {
    if url.scheme() == "tauri" {
        return true;
    }
    let Some(host) = url.host_str() else {
        return false;
    };
    // Windows 上 Tauri 资源页是 http://tauri.localhost/。
    if host == "tauri.localhost" {
        return true;
    }
    // 端口在 sidecar 就绪前是 0：加载页阶段不放行任何回环页。
    port != 0
        && url.port() == Some(port)
        && (host == "127.0.0.1" || host == "localhost")
}

/// 后台启动 sidecar：加载页已经显示，就绪后切到真实页面，再拉起研究 agent。
fn spawn_sidecar(
    app: AppHandle,
    data_dir: std::path::PathBuf,
    agent_endpoint: Option<(u16, String)>,
    sidecar_port: Arc<AtomicU16>,
) {
    std::thread::spawn(move || {
        let started = std::time::Instant::now();
        let sidecar = match Sidecar::start(&app, agent_endpoint.clone()) {
            Ok(sidecar) => sidecar,
            Err(error) => {
                paths::log_line(&app, &format!("start failed: {error}"));
                let dialog_app = app.clone();
                let message = error;
                let _ = app.run_on_main_thread(move || {
                    dialog_app
                        .dialog()
                        .message(&message)
                        .title("Paperead 启动失败")
                        .kind(MessageDialogKind::Error)
                        .blocking_show();
                    dialog_app.exit(1);
                });
                return;
            }
        };

        let port = sidecar.port;
        let token = sidecar.token.clone();
        // 先放开导航白名单，再切页面：navigate 会同步走 on_navigation。
        sidecar_port.store(port, Ordering::Relaxed);

        // 先占住句柄再切页面：切页后前端会立刻打 /api。
        let mut pending = Some(sidecar);
        let mut stored = false;
        if let Some(state) = app.try_state::<AppState>() {
            if let Ok(mut guard) = state.backend.lock() {
                if !SHUTTING_DOWN.load(Ordering::Relaxed) {
                    *guard = pending.take().map(|sidecar| Backend {
                        port,
                        token: token.clone(),
                        sidecar,
                    });
                    stored = true;
                }
            }
        }
        if let Some(mut leftover) = pending {
            // 没接管句柄（退出中或状态缺失）时必须主动结束。
            leftover.stop();
            return;
        }
        if !stored {
            return;
        }
        paths::log_line(
            &app,
            &format!("backend ready in {}ms", started.elapsed().as_millis()),
        );

        // 研究 agent 与页面切换并行：先把页面切过去，agent 继续在后台准备。
        if let Some((agent_port, agent_token)) = agent_endpoint {
            spawn_agent(
                app.clone(),
                port,
                token,
                data_dir.clone(),
                agent_port,
                agent_token,
            );
        }

        let nav_app = app.clone();
        let nav_data = data_dir;
        let target = format!("http://127.0.0.1:{port}/");
        let _ = app.run_on_main_thread(move || {
            if let Some(window) = nav_app.get_webview_window("main") {
                if let Ok(url) = Url::parse(&target) {
                    let _ = window.navigate(url);
                }
                let _ = window.show();
                let _ = window.set_focus();
            }
            paths::log_line(
                &nav_app,
                &format!(
                    "page switched in {}ms (window was already shown)",
                    started.elapsed().as_millis()
                ),
            );
            maybe_onboard(&nav_app, port, &nav_data);
        });
    });
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
    let (port, token) = match app.try_state::<AppState>() {
        Some(state) => match state.connection() {
            Ok(connection) => connection,
            Err(message) => {
                // 启动早期（双击 PDF 唤起第二实例）本地服务可能还没就绪。
                app.dialog()
                    .message(&message)
                    .title("导入 PDF")
                    .kind(MessageDialogKind::Info)
                    .show(|_| {});
                return;
            }
        },
        None => return,
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

/// 桌面端自动更新的唯一清单地址。
///
/// 帮助菜单的「检查更新」和设置页的「立即更新」都必须从 Gitee 检查与下载：
/// GitHub/镜像只作为手动下载备份，不再参与客户端更新，避免国内网络在
/// 检查或下载时退到 GitHub 长时间等待甚至失败。
const GITEE_UPDATE_MANIFEST: &str =
    "https://gitee.com/deng_xiaohui/paperead/releases/download/latest/latest.json";
const GITEE_RELEASE_PAGE: &str = "https://gitee.com/deng_xiaohui/paperead/releases";
/// 设置页订阅的更新进度事件；帮助菜单的原生弹窗不依赖它。
const UPDATE_PROGRESS_EVENT: &str = "paperead:update-progress";

/// Tauri 更新器检查到的版本信息，字段与前端 `UpdateInfo` 对齐。
#[derive(Clone, serde::Serialize)]
struct UpdateInfoPayload {
    current: String,
    latest: String,
    has_update: bool,
    notes: String,
    pub_date: String,
    url: String,
    release_url: String,
    source: String,
}

impl UpdateInfoPayload {
    fn up_to_date(app: &AppHandle) -> Self {
        let current = app.package_info().version.to_string();
        Self {
            current: current.clone(),
            latest: current,
            has_update: false,
            notes: String::new(),
            pub_date: String::new(),
            url: String::new(),
            release_url: GITEE_RELEASE_PAGE.into(),
            source: "gitee".into(),
        }
    }

    fn from_update(update: &tauri_plugin_updater::Update) -> Self {
        let raw = &update.raw_json;
        let notes = update
            .body
            .clone()
            .or_else(|| raw.get("notes").and_then(|value| value.as_str()).map(str::to_string))
            .unwrap_or_default();
        let pub_date = raw
            .get("pub_date")
            .and_then(|value| value.as_str())
            .map(str::to_string)
            .or_else(|| update.date.map(|date| date.to_string()))
            .unwrap_or_default();
        Self {
            current: update.current_version.clone(),
            latest: update.version.clone(),
            has_update: true,
            notes,
            pub_date,
            url: update.download_url.to_string(),
            release_url: GITEE_RELEASE_PAGE.into(),
            source: "gitee".into(),
        }
    }
}

/// 更新流程的阶段性进度：checking → downloading → installing → launching。
#[derive(Clone, serde::Serialize)]
struct UpdateProgress {
    stage: String,
    version: String,
    downloaded: u64,
    total: u64,
    percent: u8,
    message: String,
}

fn emit_update_progress(
    app: &AppHandle,
    stage: &str,
    version: &str,
    downloaded: u64,
    total: u64,
    message: &str,
) {
    let percent = if total > 0 {
        (downloaded.saturating_mul(100) / total).min(100) as u8
    } else {
        0
    };
    let _ = app.emit(
        UPDATE_PROGRESS_EVENT,
        UpdateProgress {
            stage: stage.into(),
            version: version.into(),
            downloaded,
            total,
            percent,
            message: message.into(),
        },
    );
}

/// 后台已下载的更新状态：idle → checking → downloading → ready → installing → launching。
///
/// `ready` 表示安装包已经完整落盘，前端只需显示一个更新图标，点击即可安装。
#[derive(Clone, Default)]
struct UpdateCenterState {
    stage: String,
    version: String,
    percent: u8,
    message: String,
}

#[derive(Default)]
struct UpdateCenter {
    inner: Mutex<UpdateCenterState>,
}

#[derive(Clone, serde::Serialize)]
struct UpdateStatusPayload {
    stage: String,
    version: String,
    percent: u8,
    message: String,
}

fn set_update_status(app: &AppHandle, stage: &str, version: &str, percent: u8, message: &str) {
    if let Some(center) = app.try_state::<UpdateCenter>() {
        if let Ok(mut guard) = center.inner.lock() {
            guard.stage = stage.into();
            guard.version = version.into();
            guard.percent = percent.min(100);
            guard.message = message.into();
        }
    }
}

fn update_stage(app: &AppHandle) -> String {
    app.try_state::<UpdateCenter>()
        .and_then(|center| center.inner.lock().ok().map(|guard| guard.stage.clone()))
        .unwrap_or_default()
}

#[tauri::command]
fn get_update_status(center: tauri::State<'_, UpdateCenter>) -> UpdateStatusPayload {
    let state = center.inner.lock().map(|guard| guard.clone()).unwrap_or_default();
    UpdateStatusPayload {
        stage: if state.stage.is_empty() { "idle".into() } else { state.stage },
        version: state.version,
        percent: state.percent,
        message: state.message,
    }
}

fn updater_for(app: &AppHandle) -> Result<tauri_plugin_updater::Updater, String> {
    // 仅保留给本机调试用的显式覆盖；正常安装包永远走 Gitee。
    let endpoint = paths::var("PAPEREAD_UPDATE_ENDPOINT");
    let url = match endpoint.as_deref() {
        Some(value) => match Url::parse(value) {
            Ok(url) => url,
            Err(error) => {
                paths::log_line(
                    app,
                    &format!("invalid PAPEREAD_UPDATE_ENDPOINT ({error}), using Gitee"),
                );
                Url::parse(GITEE_UPDATE_MANIFEST).expect("static Gitee update url")
            }
        },
        None => Url::parse(GITEE_UPDATE_MANIFEST).expect("static Gitee update url"),
    };

    app.updater_builder()
        .endpoints(vec![url])
        .map_err(|error| format!("更新检查不可用：{error}"))?
        // 安装包约 90MB：Tauri updater 默认 30 秒超时，慢网或系统代理下
        // 下载必然中断（表现为「无法检查更新 / 自动更新失败」）。
        // 给下载留 10 分钟；检查清单只有几 KB，网络正常时仍是秒回。
        .timeout(std::time::Duration::from_secs(600))
        .build()
        .map_err(|error| format!("更新检查不可用：{error}"))
}

/// 设置页「检查更新」：与帮助菜单共用同一个 Tauri 更新器，
/// 用安装包自身的版本判断，避免 sidecar 版本与桌面程序版本不同步时误判。
#[tauri::command]
async fn check_desktop_update(app: AppHandle) -> Result<UpdateInfoPayload, String> {
    let updater = updater_for(&app)?;
    match updater.check().await {
        Ok(Some(update)) => Ok(UpdateInfoPayload::from_update(&update)),
        Ok(None) => Ok(UpdateInfoPayload::up_to_date(&app)),
        Err(error) => {
            paths::log_line(&app, &format!("desktop update check failed: {error}"));
            Err(error.to_string())
        }
    }
}

/// 设置页「立即更新」：检查 → 下载（上报进度）→ 交给安装器，随后应用退出。
#[tauri::command]
async fn install_update(app: AppHandle) -> Result<String, String> {
    let updater = updater_for(&app)?;
    emit_update_progress(&app, "checking", "", 0, 0, "正在检查更新…");
    let update = match updater.check().await {
        Ok(Some(update)) => update,
        Ok(None) => {
            let message = "当前已是最新版本".to_string();
            emit_update_progress(&app, "up-to-date", "", 0, 0, &message);
            return Err(message);
        }
        Err(error) => {
            let message = format!("检查更新失败：{error}");
            paths::log_line(&app, &format!("manual update check failed: {error}"));
            emit_update_progress(&app, "failed", "", 0, 0, &message);
            return Err(message);
        }
    };

    let version = update.version.clone();

    // 后台已经下好这个版本：直接启动安装器，不再重复下载约 90MB。
    if let Some((temp_dir, installer, _)) = existing_installer(&app, &version) {
        paths::log_line(&app, &format!("reuse prepared update: {version}"));
        emit_update_progress(&app, "installing", &version, 0, 0, "正在启动安装程序…");
        set_update_status(&app, "installing", &version, 100, "正在启动安装程序…");
        launch_update_installer(&app, &version, &temp_dir, &installer)?;
        return Ok(version);
    }

    paths::log_line(&app, &format!("manual update download started: {version}"));
    emit_update_progress(&app, "downloading", &version, 0, 0, "正在下载安装包…");

    let progress_app = app.clone();
    let progress_version = version.clone();
    let mut downloaded: u64 = 0;
    let mut last_emit = std::time::Instant::now();
    let bytes = match update
        .download(
            move |chunk, total| {
                downloaded += chunk as u64;
                // 每 ~120ms 上报一次，避免大包下载时把 WebView 事件队列塞满。
                if last_emit.elapsed().as_millis() >= 120 {
                    last_emit = std::time::Instant::now();
                    emit_update_progress(
                        &progress_app,
                        "downloading",
                        &progress_version,
                        downloaded,
                        total.unwrap_or(0),
                        "正在下载安装包…",
                    );
                }
            },
            || {},
        )
        .await
    {
        Ok(bytes) => bytes,
        Err(error) => {
            let message = format!("下载更新失败：{error}");
            paths::log_line(&app, &format!("manual update download failed: {error}"));
            emit_update_progress(&app, "failed", &version, 0, 0, &message);
            return Err(message);
        }
    };

    emit_update_progress(&app, "installing", &version, 0, 0, "正在校验安装包…");
    if let Err(error) = install_downloaded_update(&app, &version, &bytes) {
        emit_update_progress(&app, "failed", &version, 0, 0, &error);
        return Err(error);
    }
    Ok(version)
}

/// 侧栏更新图标：用后台已经下载好的安装包直接启动安装器。
///
/// 没有准备好的安装包时返回 `not-ready`，前端会回退到完整更新流程。
#[tauri::command]
async fn install_prepared_update(app: AppHandle) -> Result<String, String> {
    let version = app
        .try_state::<UpdateCenter>()
        .and_then(|center| center.inner.lock().ok().map(|guard| guard.version.clone()))
        .unwrap_or_default();

    let target = if version.is_empty() {
        scan_prepared_installer(&app)
    } else {
        existing_installer(&app, &version).or_else(|| scan_prepared_installer(&app))
    };

    let Some((temp_dir, installer, version)) = target else {
        return Err("not-ready".into());
    };

    paths::log_line(&app, &format!("install prepared update: {version}"));
    emit_update_progress(&app, "installing", &version, 0, 0, "正在启动安装程序…");
    set_update_status(&app, "installing", &version, 100, "正在启动安装程序…");
    launch_update_installer(&app, &version, &temp_dir, &installer)?;
    Ok(version)
}

/// 后台准备：检查 → 下载 → 落盘，成功后状态置 `ready` 并广播事件。
///
/// 前端不可见时也会跑（由启动后的定时任务驱动），用户看到更新图标时安装包已在本地。
#[cfg(windows)]
async fn prepare_update_once(app: &AppHandle) {
    if matches!(
        update_stage(app).as_str(),
        "checking" | "downloading" | "installing" | "launching"
    ) {
        return;
    }

    set_update_status(app, "checking", "", 0, "正在检查更新…");

    let updater = match updater_for(app) {
        Ok(updater) => updater,
        Err(error) => {
            paths::log_line(app, &format!("background updater init failed: {error}"));
            restore_prepared_status(app);
            return;
        }
    };

    let update = match updater.check().await {
        Ok(Some(update)) => update,
        Ok(None) => {
            // 当前已是最新：清掉旧版本安装包，避免侧栏残留「可安装」入口。
            if let Some((_, path, _)) = scan_prepared_installer(app) {
                let _ = std::fs::remove_file(&path);
            }
            set_update_status(app, "up-to-date", "", 100, "当前已是最新版本");
            emit_update_progress(app, "up-to-date", "", 0, 0, "当前已是最新版本");
            return;
        }
        Err(error) => {
            paths::log_line(app, &format!("background update check failed: {error}"));
            restore_prepared_status(app);
            return;
        }
    };

    let version = update.version.clone();
    if existing_installer(app, &version).is_some() {
        paths::log_line(app, &format!("prepared update already on disk: {version}"));
        mark_update_ready(app, &version);
        return;
    }

    paths::log_line(app, &format!("background update download started: {version}"));
    set_update_status(app, "downloading", &version, 0, "正在下载安装包…");
    emit_update_progress(app, "downloading", &version, 0, 0, "正在下载安装包…");

    let progress_app = app.clone();
    let progress_version = version.clone();
    let mut downloaded: u64 = 0;
    let mut last_emit = std::time::Instant::now();
    let bytes = match update
        .download(
            move |chunk, total| {
                downloaded += chunk as u64;
                // 与大包下载一致：~120ms 一次，避免事件队列被进度塞满。
                if last_emit.elapsed().as_millis() >= 120 {
                    last_emit = std::time::Instant::now();
                    let total = total.unwrap_or(0);
                    let percent = if total > 0 {
                        (downloaded.saturating_mul(100) / total).min(100) as u8
                    } else {
                        0
                    };
                    set_update_status(
                        &progress_app,
                        "downloading",
                        &progress_version,
                        percent,
                        "正在下载安装包…",
                    );
                    emit_update_progress(
                        &progress_app,
                        "downloading",
                        &progress_version,
                        downloaded,
                        total,
                        "正在下载安装包…",
                    );
                }
            },
            || {},
        )
        .await
    {
        Ok(bytes) => bytes,
        Err(error) => {
            // 下载失败保持静默：下次启动或下一个检查周期会重试。
            paths::log_line(app, &format!("background update download failed: {error}"));
            restore_prepared_status(app);
            return;
        }
    };

    match write_update_files(app, &version, &bytes) {
        Ok(_) => {
            mark_update_ready(app, &version);
            paths::log_line(app, &format!("background update prepared: {version}"));
        }
        Err(error) => {
            paths::log_line(app, &format!("background update save failed: {error}"));
            restore_prepared_status(app);
        }
    }
}

#[cfg(not(windows))]
async fn prepare_update_once(_app: &AppHandle) {}

fn mark_update_ready(app: &AppHandle, version: &str) {
    set_update_status(app, "ready", version, 100, "新版本已下载，点击安装");
    emit_update_progress(app, "ready", version, 0, 0, "新版本已下载，点击安装");
}

/// 失败/取消后回到「已有安装包就仍是 ready，否则 idle」。
fn restore_prepared_status(app: &AppHandle) {
    match scan_prepared_installer(app) {
        Some((_, _, version)) => mark_update_ready(app, &version),
        None => set_update_status(app, "idle", "", 0, ""),
    }
}

/// 设置页更改文献库保存路径后：数据已复制完成，重启应用让 sidecar 指向新目录。
#[tauri::command]
fn restart_app(app: AppHandle) {
    app.restart();
}

/// 文献库「下载 PDF」：路径由系统保存对话框让用户自己选，壳直接拉取 sidecar
/// 里的原文件写盘，避免 WebView 默认下载到浏览器下载目录。
#[tauri::command]
async fn save_pdf_file(
    state: tauri::State<'_, AppState>,
    paper_id: String,
    path: String,
) -> Result<String, String> {
    let (port, token) = state.connection()?;
    let target = path.clone();
    tauri::async_runtime::spawn_blocking(move || -> Result<(), String> {
        let url = format!("http://127.0.0.1:{port}/api/papers/{paper_id}/file");
        let response = ureq::get(&url)
            .set("x-paperead-token", &token)
            // 大论文可能几十 MB，给足时间，避免慢盘或代理下中途截断。
            .timeout(std::time::Duration::from_secs(600))
            .call()
            .map_err(|error| match error {
                ureq::Error::Status(code, _) => format!("下载失败（HTTP {code}）"),
                other => format!("下载失败：{other}"),
            })?;
        let mut bytes = Vec::new();
        use std::io::Read;
        response
            .into_reader()
            .read_to_end(&mut bytes)
            .map_err(|error| format!("下载中断：{error}"))?;
        std::fs::write(&target, &bytes)
            .map_err(|error| format!("无法写入文件：{error}"))?;
        Ok(())
    })
    .await
    .map_err(|error| format!("保存失败：{error}"))??;
    Ok(path)
}

/// 双栏「下载译文 PDF」：同样由系统保存对话框选位置，壳直接拉取 sidecar
/// 的页级回填产物写盘，避免 WebView 默认下载目录。
#[tauri::command]
async fn save_translated_pdf(
    state: tauri::State<'_, AppState>,
    paper_id: String,
    lang: String,
    path: String,
) -> Result<String, String> {
    let (port, token) = state.connection()?;
    let target = path.clone();
    tauri::async_runtime::spawn_blocking(move || -> Result<(), String> {
        let url = format!(
            "http://127.0.0.1:{port}/api/papers/{paper_id}/translated-pdf?lang={lang}"
        );
        let response = ureq::get(&url)
            .set("x-paperead-token", &token)
            .timeout(std::time::Duration::from_secs(600))
            .call()
            .map_err(|error| match error {
                ureq::Error::Status(code, _) => format!("下载译文失败（HTTP {code}）"),
                other => format!("下载译文失败：{other}"),
            })?;
        let mut bytes = Vec::new();
        use std::io::Read;
        response
            .into_reader()
            .read_to_end(&mut bytes)
            .map_err(|error| format!("下载中断：{error}"))?;
        std::fs::write(&target, &bytes)
            .map_err(|error| format!("无法写入文件：{error}"))?;
        Ok(())
    })
    .await
    .map_err(|error| format!("保存失败：{error}"))??;
    Ok(path)
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

            // 已经下好安装包：先问用户是否现在安装，同意后才启动安装器。
            if let Some((temp_dir, installer, prepared_version)) =
                existing_installer(&app, &version)
            {
                paths::log_line(
                    &app,
                    &format!("check-updates: reuse prepared {prepared_version}"),
                );
                prompt_install_after_ready(&app, &prepared_version, &temp_dir, &installer);
                return;
            }

            if !interactive {
                prepare_update_once(&app).await;
                return;
            }

            // 后台正在下载：等它下完再装，避免重复下载约 90MB。
            if matches!(update_stage(&app).as_str(), "downloading" | "checking") {
                notify_download_in_progress(&app, &version);
                match wait_for_prepared_update(&app, &version).await {
                    Some((temp_dir, installer, prepared_version)) => {
                        prompt_install_after_ready(
                            &app,
                            &prepared_version,
                            &temp_dir,
                            &installer,
                        );
                    }
                    None => {
                        app.dialog()
                            .message(format!(
                                "新版本 {version} 下载未完成，可稍后重新检查更新，或在左下角点击更新图标重试。"
                            ))
                            .title("检查更新")
                            .kind(MessageDialogKind::Info)
                            .show(|_| {});
                    }
                }
                return;
            }

            // 没下好就现场下：只提示预计耗时，下载完自动安装，不需要用户再操作。
            notify_download_in_progress(&app, &version);
            paths::log_line(&app, &format!("check-updates: downloading {version}"));

            set_update_status(&app, "downloading", &version, 0, "正在下载安装包…");
            emit_update_progress(&app, "downloading", &version, 0, 0, "正在下载安装包…");

            let progress_app = app.clone();
            let progress_version = version.clone();
            let mut downloaded: u64 = 0;
            let mut last_emit = std::time::Instant::now();
            let bytes = match update
                .download(
                    move |chunk, total| {
                        downloaded += chunk as u64;
                        if last_emit.elapsed().as_millis() >= 120 {
                            last_emit = std::time::Instant::now();
                            let total = total.unwrap_or(0);
                            let percent = if total > 0 {
                                (downloaded.saturating_mul(100) / total).min(100) as u8
                            } else {
                                0
                            };
                            set_update_status(
                                &progress_app,
                                "downloading",
                                &progress_version,
                                percent,
                                "正在下载安装包…",
                            );
                            emit_update_progress(
                                &progress_app,
                                "downloading",
                                &progress_version,
                                downloaded,
                                total,
                                "正在下载安装包…",
                            );
                        }
                    },
                    || {},
                )
                .await
            {
                Ok(bytes) => bytes,
                Err(error) => {
                    paths::log_line(&app, &format!("check-updates download failed: {error}"));
                    set_update_status(&app, "idle", "", 0, "");
                    emit_update_progress(&app, "failed", &version, 0, 0, "下载更新失败");
                    app.dialog()
                        .message(format!("下载新版本失败：{error}"))
                        .title("检查更新")
                        .kind(MessageDialogKind::Error)
                        .show(|_| {});
                    return;
                }
            };

            match save_update_package(&app, &version, &bytes) {
                Ok((temp_dir, installer)) => {
                    mark_update_ready(&app, &version);
                    paths::log_line(
                        &app,
                        &format!("check-updates: prepared {version}, asking to install"),
                    );
                    prompt_install_after_ready(&app, &version, &temp_dir, &installer);
                }
                Err(error) => {
                    set_update_status(&app, "idle", "", 0, "");
                    emit_update_progress(&app, "failed", &version, 0, 0, "保存安装包失败");
                    app.dialog()
                        .message(format!("更新失败：{error}"))
                        .title("更新失败")
                        .kind(MessageDialogKind::Error)
                        .show(|_| {});
                }
            }
        }
        Ok(None) => {
            if interactive {
                app.dialog()
                    .message("当前已是最新版本（Gitee）。")
                    .title("检查更新")
                    .kind(MessageDialogKind::Info)
                    .show(|_| {});
            }
        }
        Err(error) => {
            paths::log_line(&app, &format!("update check failed: {error}"));
            if interactive {
                app.dialog()
                    .message(format!("从 Gitee 检查更新失败：{error}"))
                    .title("检查更新")
                    .kind(MessageDialogKind::Error)
                    .show(|_| {});
            }
        }
    }
}

/// 用户主动「检查更新」时的下载提示：只说预计耗时与「无需手动操作」，
/// 下载完成后由调用方直接进入安装，不再弹二次确认。
fn notify_download_in_progress(app: &AppHandle, version: &str) {
    app.dialog()
        .message(format!(
            "新版本 {version} 正在下载（安装包约 90 MB，视网络情况通常需要 1–3 分钟）。\n\n下载完成后会询问你是否立即安装，期间无需手动操作。"
        ))
        .title("检查更新")
        .kind(MessageDialogKind::Info)
        .show(|_| {});
}

/// 安装包已就绪后才弹「是否立即安装」：用户同意才启动安装器。
///
/// 取消时保留已下载的安装包，左下角的更新入口仍可再次触发安装。
fn prompt_install_after_ready(
    app: &AppHandle,
    version: &str,
    temp_dir: &std::path::Path,
    installer: &std::path::Path,
) {
    let app_for_dialog = app.clone();
    let version = version.to_string();
    let temp_dir = temp_dir.to_path_buf();
    let installer = installer.to_path_buf();
    app.dialog()
        .message(format!(
            "新版本 {version} 的安装包已下载完成。\n\n是否立即安装？安装期间应用会关闭，完成后自动打开。"
        ))
        .title("发现更新")
        .buttons(MessageDialogButtons::OkCancel)
        .show(move |confirmed| {
            if !confirmed {
                paths::log_line(
                    &app_for_dialog,
                    &format!("update install skipped: {version}"),
                );
                return;
            }
            begin_update_install(&app_for_dialog, &version, &temp_dir, &installer);
        });
}

/// 启动安装器：上报阶段状态，失败时回到「可重试」并提示原因。
fn begin_update_install(
    app: &AppHandle,
    version: &str,
    temp_dir: &std::path::Path,
    installer: &std::path::Path,
) {
    emit_update_progress(app, "installing", version, 0, 0, "正在启动安装程序…");
    set_update_status(app, "installing", version, 100, "正在启动安装程序…");
    if let Err(error) = launch_update_installer(app, version, temp_dir, installer) {
        set_update_status(app, "ready", version, 100, "安装失败，点击重试");
        app.dialog()
            .message(format!("启动安装程序失败：{error}"))
            .title("更新失败")
            .kind(MessageDialogKind::Error)
            .show(|_| {});
    }
}

/// 后台已在下载时，等待它落盘（不重复下载一份），最多等 15 分钟。
#[cfg(windows)]
async fn wait_for_prepared_update(
    app: &AppHandle,
    version: &str,
) -> Option<(std::path::PathBuf, std::path::PathBuf, String)> {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(900);
    loop {
        if let Some(found) = existing_installer(app, version) {
            return Some(found);
        }
        let stage = update_stage(app);
        // 后台下载已结束却没产出安装包：失败或已是最新，直接返回让调用方提示。
        if stage != "downloading" && stage != "checking" {
            return None;
        }
        if std::time::Instant::now() >= deadline {
            return None;
        }
        tokio::time::sleep(std::time::Duration::from_millis(800)).await;
    }
}

#[cfg(not(windows))]
async fn wait_for_prepared_update(
    _app: &AppHandle,
    _version: &str,
) -> Option<(std::path::PathBuf, std::path::PathBuf, String)> {
    None
}

#[cfg(windows)]
fn save_update_package(
    app: &AppHandle,
    version: &str,
    bytes: &[u8],
) -> Result<(std::path::PathBuf, std::path::PathBuf), String> {
    write_update_files(app, version, bytes)
}

#[cfg(not(windows))]
fn save_update_package(
    _app: &AppHandle,
    _version: &str,
    _bytes: &[u8],
) -> Result<(std::path::PathBuf, std::path::PathBuf), String> {
    Err("当前平台暂不支持自动安装更新".into())
}

#[cfg(windows)]
fn update_temp_dirs(app: &AppHandle) -> Vec<std::path::PathBuf> {
    let primary = paths::ensure_data_dir(app).join("update-temp");
    let system = std::env::temp_dir().join("paperead-update");
    if system == primary {
        vec![primary]
    } else {
        vec![primary, system]
    }
}

/// 依次尝试数据目录与系统临时目录，避免某一个目录被 ACL/杀软限制时整条更新链失败。
#[cfg(windows)]
fn write_update_files(
    app: &AppHandle,
    version: &str,
    bytes: &[u8],
) -> Result<(std::path::PathBuf, std::path::PathBuf), String> {
    let mut errors: Vec<String> = Vec::new();
    for dir in update_temp_dirs(app) {
        if let Err(error) = std::fs::create_dir_all(&dir) {
            errors.push(format!("创建 {} 失败：{error}", dir.display()));
            continue;
        }
        let installer = dir.join(format!("paperead-{version}-setup.exe"));
        // 上一次残留的安装包可能仍被安装器占用，先尽力删掉再写。
        let _ = std::fs::remove_file(&installer);
        match std::fs::write(&installer, bytes) {
            Ok(()) => return Ok((dir, installer)),
            Err(error) => errors.push(format!("写入 {} 失败：{error}", installer.display())),
        }
    }
    let message = format!(
        "写入更新包失败（已尝试 {} 个目录）：{}",
        errors.len(),
        errors.join("；")
    );
    paths::log_line(app, &message);
    Err(message)
}

/// 版本号比较键：非数字片段按 0 处理，避免 `0.2.10` 被当成小于 `0.2.9`。
fn version_key(value: &str) -> Vec<u64> {
    value
        .split(['.', '-', '+'])
        .map(|part| part.parse::<u64>().unwrap_or(0))
        .collect()
}

/// 指定版本的安装包是否已经完整落在本机（后台下载完成后在这里命中）。
#[cfg(windows)]
fn existing_installer(
    app: &AppHandle,
    version: &str,
) -> Option<(std::path::PathBuf, std::path::PathBuf, String)> {
    for dir in update_temp_dirs(app) {
        let installer = dir.join(format!("paperead-{version}-setup.exe"));
        if let Ok(meta) = std::fs::metadata(&installer) {
            // 小于 1MB 视为上次中断留下的残片，重新下载。
            if meta.len() > 1_000_000 {
                return Some((dir, installer, version.to_string()));
            }
        }
    }
    None
}

#[cfg(not(windows))]
fn existing_installer(
    _app: &AppHandle,
    _version: &str,
) -> Option<(std::path::PathBuf, std::path::PathBuf, String)> {
    None
}

/// 扫描更新目录里任意已准备好的安装包，返回 (目录, 安装包, 版本)。
#[cfg(windows)]
fn scan_prepared_installer(
    app: &AppHandle,
) -> Option<(std::path::PathBuf, std::path::PathBuf, String)> {
    const PREFIX: &str = "paperead-";
    const SUFFIX: &str = "-setup.exe";
    let mut best: Option<(std::path::PathBuf, std::path::PathBuf, String)> = None;

    for dir in update_temp_dirs(app) {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let name = match path.file_name().and_then(|name| name.to_str()) {
                Some(name) => name.to_string(),
                None => continue,
            };
            if !name.starts_with(PREFIX) || !name.ends_with(SUFFIX) {
                continue;
            }
            let version = name[PREFIX.len()..name.len() - SUFFIX.len()].to_string();
            if version.is_empty() {
                continue;
            }
            let size = entry.metadata().map(|meta| meta.len()).unwrap_or(0);
            if size <= 1_000_000 {
                continue;
            }
            let higher = best
                .as_ref()
                .map(|(_, _, current)| version_key(&version) > version_key(current))
                .unwrap_or(true);
            if higher {
                best = Some((dir.clone(), path, version));
            }
        }
    }
    best
}

#[cfg(not(windows))]
fn scan_prepared_installer(
    _app: &AppHandle,
) -> Option<(std::path::PathBuf, std::path::PathBuf, String)> {
    None
}

#[cfg(windows)]
fn install_downloaded_update(
    app: &AppHandle,
    version: &str,
    bytes: &[u8],
) -> Result<(), String> {
    let (temp_dir, installer) = write_update_files(app, version, bytes)?;
    launch_update_installer(app, version, &temp_dir, &installer)
}

/// 启动已经写盘的安装器：批处理延迟拉起 NSIS，随后应用退出。
///
/// 后台预下载与「立即更新」共用这一段，避免两条链路各自维护启动脚本。
#[cfg(windows)]
fn launch_update_installer(
    app: &AppHandle,
    version: &str,
    temp_dir: &std::path::Path,
    installer: &std::path::Path,
) -> Result<(), String> {
    use std::os::windows::process::CommandExt;

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
    if let Err(error) = std::fs::write(&batch, batch_content) {
        let message = format!("写入更新脚本失败：{error}");
        paths::log_line(app, &message);
        return Err(message);
    }
    std::process::Command::new(cmd)
        .env("TEMP", temp_dir)
        .env("TMP", temp_dir)
        .args(["/C", &batch.to_string_lossy()])
        .creation_flags(0x0800_0000)
        .spawn()
        .map_err(|error| {
            let message = format!("启动更新安装器失败：{error}");
            paths::log_line(app, &message);
            message
        })?;

    paths::log_line(
        app,
        &format!(
            "update installer scheduled: {} (temp={})",
            installer.display(),
            temp_dir.display()
        ),
    );
    // 先让命令返回、前端收到「安装程序已启动」，稍后再退出：退出得太快会让
    // invoke 的 Promise 随 WebView 销毁被拒绝，被前端误判成「更新失败」。
    emit_update_progress(
        app,
        "launching",
        version,
        0,
        0,
        "安装程序已启动，应用即将退出…",
    );
    let handle = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(3000));
        handle.exit(0);
    });
    Ok(())
}

#[cfg(not(windows))]
fn launch_update_installer(
    _app: &AppHandle,
    _version: &str,
    _temp_dir: &std::path::Path,
    _installer: &std::path::Path,
) -> Result<(), String> {
    Err("当前平台暂不支持自动安装更新".into())
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
    // 先立退出标记：后台 agent 线程若此刻刚好启动成功，会自己收掉句柄。
    SHUTTING_DOWN.store(true, Ordering::Relaxed);
    if let Some(state) = app.try_state::<AppState>() {
        if let Ok(mut guard) = state.agent.lock() {
            if let Some(mut agent) = guard.take() {
                agent.stop();
            }
        }
        if let Ok(mut guard) = state.backend.lock() {
            if let Some(mut backend) = guard.take() {
                backend.sidecar.stop();
            }
        }
    }
}

