//! Android 应用内更新：调用 Kotlin 插件检查版本、下载 APK 并调起系统安装器。
//!
//! 桌面端继续使用 `tauri-plugin-updater`，这里只在手机壳里注册。

use tauri::{
    plugin::{Builder, TauriPlugin},
    AppHandle, Manager, Runtime,
};

#[cfg(target_os = "android")]
use tauri::plugin::PluginHandle;

#[cfg(target_os = "android")]
const PLUGIN_IDENTIFIER: &str = "com.paperead.desktop";
#[cfg(target_os = "android")]
const PLUGIN_CLASS: &str = "ApkUpdaterPlugin";

pub struct ApkUpdater<R: Runtime> {
    #[cfg(target_os = "android")]
    handle: PluginHandle<R>,
    #[cfg(not(target_os = "android"))]
    _marker: std::marker::PhantomData<R>,
}

// 手写 Clone：derive 会给 R 加 Clone 约束，而 Wry 运行时不实现 Clone。
#[cfg(target_os = "android")]
impl<R: Runtime> Clone for ApkUpdater<R> {
    fn clone(&self) -> Self {
        Self {
            handle: self.handle.clone(),
        }
    }
}

#[cfg(not(target_os = "android"))]
impl<R: Runtime> Clone for ApkUpdater<R> {
    fn clone(&self) -> Self {
        Self {
            _marker: std::marker::PhantomData,
        }
    }
}

pub fn init<R: Runtime>() -> TauriPlugin<R> {
    Builder::new("apk-updater")
        .setup(|app, api| {
            #[cfg(target_os = "android")]
            {
                let handle = api.register_android_plugin(PLUGIN_IDENTIFIER, PLUGIN_CLASS)?;
                app.manage(ApkUpdater { handle });
            }
            #[cfg(not(target_os = "android"))]
            {
                let _ = api;
                app.manage(ApkUpdater::<R> {
                    _marker: std::marker::PhantomData,
                });
            }
            Ok(())
        })
        .build()
}

/// 依次请求更新清单端点，返回第一个可用清单的解析结果。
#[tauri::command]
pub async fn check_apk_update(
    app: AppHandle<tauri::Wry>,
    endpoints: Vec<String>,
) -> Result<serde_json::Value, String> {
    let updater = app.state::<ApkUpdater<tauri::Wry>>().inner().clone();
    #[cfg(target_os = "android")]
    {
        updater
            .handle
            .run_mobile_plugin_async::<serde_json::Value>(
                "checkUpdate",
                serde_json::json!({ "endpoints": endpoints }),
            )
            .await
            .map_err(|e| e.to_string())
    }
    #[cfg(not(target_os = "android"))]
    {
        let _ = (updater, endpoints);
        Err("当前平台不支持 APK 更新".to_string())
    }
}

/// 下载 APK 并调起系统安装器；下载期间命令保持挂起。
#[tauri::command]
pub async fn install_apk_update(
    app: AppHandle<tauri::Wry>,
    url: String,
    fallback_urls: Option<Vec<String>>,
    sha256: Option<String>,
) -> Result<serde_json::Value, String> {
    let updater = app.state::<ApkUpdater<tauri::Wry>>().inner().clone();
    #[cfg(target_os = "android")]
    {
        updater
            .handle
            .run_mobile_plugin_async::<serde_json::Value>(
                "installUpdate",
                serde_json::json!({
                    "url": url,
                    "fallbackUrls": fallback_urls.unwrap_or_default(),
                    "sha256": sha256.unwrap_or_default(),
                }),
            )
            .await
            .map_err(|e| e.to_string())
    }
    #[cfg(not(target_os = "android"))]
    {
        let _ = (updater, url, fallback_urls, sha256);
        Err("当前平台不支持 APK 更新".to_string())
    }
}
