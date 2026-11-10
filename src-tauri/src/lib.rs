#[cfg(desktop)]
mod desktop;

#[cfg(mobile)]
mod llm_stream;

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
        // llm_stream 让前端在连不上后端时也能直连模型并保持流式输出。
        tauri::Builder::default()
            .plugin(tauri_plugin_barcode_scanner::init())
            .manage(llm_stream::Cancels::default())
            .invoke_handler(tauri::generate_handler![
                llm_stream::llm_stream,
                llm_stream::llm_cancel
            ])
            .run(tauri::generate_context!())
            .expect("failed to run Paperead mobile");
    }
}
