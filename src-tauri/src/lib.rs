#[cfg(desktop)]
mod desktop;

/// 桌面端：启动本机 sidecar、研究 agent 与更新器。
/// 安卓端：内置前端 + 用户配置的远程后端，不需要 sidecar。
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    #[cfg(desktop)]
    desktop::run_desktop();

    #[cfg(mobile)]
    {
        // 手机端只用 WebView 能力（文件选择走 <input type="file">），
        // 不需要 dialog/opener/updater 等桌面插件。
        tauri::Builder::default()
            .run(tauri::generate_context!())
            .expect("failed to run Paperead mobile");
    }
}
