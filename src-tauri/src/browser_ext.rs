// 浏览器扩展安装支持。
//
// 桌面壳里没有内置浏览器扩展的自动装载能力：Chrome / Edge 只允许从官方
// 商店静默安装扩展，本地未上架的扩展必须由用户在扩展管理页手动加载。
// 这里做能自动化的部分：检测本机浏览器、把安装包内的扩展释放到固定目录，
// 并帮用户直接打开对应浏览器的扩展管理页；剩下三步由前端教程引导。

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde::Serialize;
use tauri::{AppHandle, Manager};
use tauri_plugin_opener::OpenerExt;

/// 扩展的两种产物：Chromium 系用 MV3，Firefox 用 MV2。
const CHROMIUM_FLAVOR: &str = "chrome-mv3";
const FIREFOX_FLAVOR: &str = "firefox-mv2";
const STAMP_FILE: &str = ".paperead-version";

#[derive(Serialize, Clone)]
pub struct BrowserInfo {
    pub id: String,
    pub name: String,
    /// `chromium` 或 `firefox`，决定加载哪一份扩展产物与教程。
    pub family: String,
    pub path: String,
    /// 该浏览器的扩展管理页地址。
    pub extensions_url: String,
}

#[derive(Serialize)]
pub struct ExtensionSetup {
    pub version: String,
    pub chromium_dir: Option<String>,
    pub firefox_dir: Option<String>,
    pub browsers: Vec<BrowserInfo>,
}

struct BrowserSpec {
    id: &'static str,
    name: &'static str,
    family: &'static str,
    /// 相对 Program Files / LOCALAPPDATA 的可执行文件路径。
    relative: &'static str,
    /// 各安装位置的环境变量名，按优先级排列。
    bases: &'static [&'static str],
    extensions_url: &'static str,
}

const BROWSERS: &[BrowserSpec] = &[
    BrowserSpec {
        id: "chrome",
        name: "Google Chrome",
        family: "chromium",
        relative: r"Google\Chrome\Application\chrome.exe",
        bases: &["ProgramFiles", "ProgramFiles(x86)", "LocalAppData"],
        extensions_url: "chrome://extensions/",
    },
    BrowserSpec {
        id: "edge",
        name: "Microsoft Edge",
        family: "chromium",
        relative: r"Microsoft\Edge\Application\msedge.exe",
        bases: &["ProgramFiles(x86)", "ProgramFiles", "LocalAppData"],
        extensions_url: "edge://extensions/",
    },
    BrowserSpec {
        id: "brave",
        name: "Brave",
        family: "chromium",
        relative: r"BraveSoftware\Brave-Browser\Application\brave.exe",
        bases: &["ProgramFiles", "ProgramFiles(x86)", "LocalAppData"],
        extensions_url: "brave://extensions/",
    },
    BrowserSpec {
        id: "firefox",
        name: "Mozilla Firefox",
        family: "firefox",
        relative: r"Mozilla Firefox\firefox.exe",
        bases: &["ProgramFiles", "ProgramFiles(x86)"],
        extensions_url: "about:debugging#/runtime/this-firefox",
    },
];

/// 去掉 Windows 长路径前缀，Chrome / 资源管理器读取时更稳。
fn plain_path(path: PathBuf) -> PathBuf {
    let text = path.to_string_lossy();
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        return PathBuf::from(format!(r"\\{rest}"));
    }
    if let Some(rest) = text.strip_prefix(r"\\?\") {
        return PathBuf::from(rest.to_string());
    }
    path
}

fn detect_browsers() -> Vec<BrowserInfo> {
    let mut found = Vec::new();

    for spec in BROWSERS {
        let mut resolved: Option<PathBuf> = None;
        for base in spec.bases {
            let Some(root) = std::env::var_os(base).map(PathBuf::from) else {
                continue;
            };
            let candidate = root.join(spec.relative);
            if candidate.is_file() {
                resolved = Some(candidate);
                break;
            }
        }

        let Some(path) = resolved else {
            continue;
        };

        found.push(BrowserInfo {
            id: spec.id.to_string(),
            name: spec.name.to_string(),
            family: spec.family.to_string(),
            path: path.to_string_lossy().to_string(),
            extensions_url: spec.extensions_url.to_string(),
        });
    }

    found
}

/// 安装包里的扩展产物根目录（其下是 `chrome-mv3` / `firefox-mv2`）。
fn resource_root(app: &AppHandle) -> Option<PathBuf> {
    if let Ok(resource) = app.path().resource_dir() {
        for base in [resource.clone(), resource.join("resources")] {
            let candidate = base.join("browser-extension");
            if candidate.is_dir() {
                return Some(plain_path(candidate));
            }
        }
    }

    #[cfg(debug_assertions)]
    {
        let dev = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../extensions/browser/.output");
        if dev.is_dir() {
            return Some(dev);
        }
    }

    None
}

fn manifest_version(dir: &Path) -> Option<String> {
    let text = fs::read_to_string(dir.join("manifest.json")).ok()?;
    let value: serde_json::Value = serde_json::from_str(&text).ok()?;
    value.get("version")?.as_str().map(str::to_string)
}

fn copy_dir_all(src: &Path, dst: &Path) -> std::io::Result<()> {
    fs::create_dir_all(dst)?;
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        let target = dst.join(entry.file_name());
        if file_type.is_dir() {
            copy_dir_all(&entry.path(), &target)?;
        } else {
            fs::copy(entry.path(), &target)?;
        }
    }
    Ok(())
}

/// 把某个产物目录释放到用户数据目录；版本没变时直接复用，避免每次启动都
/// 复制几十个文件。返回释放后的目录。
fn release_flavor(root: &Path, flavor: &str, target_root: &Path) -> Option<PathBuf> {
    let source = root.join(flavor);
    if !source.is_dir() {
        return None;
    }

    let version = manifest_version(&source).unwrap_or_else(|| "unknown".to_string());
    let target = target_root.join(flavor);
    let stamp = target.join(STAMP_FILE);
    let ready = target.join("manifest.json").is_file()
        && fs::read_to_string(&stamp)
            .map(|value| value.trim() == version)
            .unwrap_or(false);

    if ready {
        return Some(target);
    }

    if target.exists() {
        let _ = fs::remove_dir_all(&target);
    }
    if copy_dir_all(&source, &target).is_err() {
        return None;
    }
    let _ = fs::write(&stamp, version);
    Some(target)
}

fn extension_root(app: &AppHandle) -> PathBuf {
    match app.path().app_local_data_dir() {
        Ok(dir) => plain_path(dir).join("browser-extension"),
        Err(_) => std::env::temp_dir().join("paperead-browser-extension"),
    }
}

pub fn prepare(app: &AppHandle) -> ExtensionSetup {
    let browsers = detect_browsers();
    let mut setup = ExtensionSetup {
        version: String::new(),
        chromium_dir: None,
        firefox_dir: None,
        browsers,
    };

    let Some(root) = resource_root(app) else {
        return setup;
    };

    let target_root = extension_root(app);
    if let Some(dir) = release_flavor(&root, CHROMIUM_FLAVOR, &target_root) {
        setup.version = manifest_version(&dir).unwrap_or_default();
        setup.chromium_dir = Some(dir.to_string_lossy().to_string());
    }
    if let Some(dir) = release_flavor(&root, FIREFOX_FLAVOR, &target_root) {
        if setup.version.is_empty() {
            setup.version = manifest_version(&dir).unwrap_or_default();
        }
        setup.firefox_dir = Some(dir.to_string_lossy().to_string());
    }

    setup
}

#[tauri::command]
pub fn browser_extension_setup(app: AppHandle) -> ExtensionSetup {
    prepare(&app)
}

#[tauri::command]
pub fn open_browser_extensions(id: String) -> Result<(), String> {
    let browsers = detect_browsers();
    let target = browsers
        .iter()
        .find(|browser| browser.id == id)
        .ok_or_else(|| format!("没有找到浏览器：{id}"))?;

    Command::new(&target.path)
        .arg(&target.extensions_url)
        .spawn()
        .map_err(|error| format!("打开 {} 失败：{error}", target.name))?;
    Ok(())
}

#[tauri::command]
pub fn open_extension_folder(app: AppHandle, family: String) -> Result<(), String> {
    let setup = prepare(&app);
    let dir = if family == "firefox" {
        setup.firefox_dir
    } else {
        setup.chromium_dir
    }
    .ok_or_else(|| "扩展还没有准备好，请重新安装 Paperead".to_string())?;

    app.opener()
        .open_path(dir.clone(), None::<&str>)
        .map_err(|error| format!("打开扩展目录失败：{error}"))
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_root(tag: &str) -> PathBuf {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        // 本机系统临时目录不可写，测试目录放到 target/ 下。
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-tmp")
            .join(format!("paperead-ext-test-{tag}-{stamp}"));
        let _ = fs::remove_dir_all(&dir);
        dir
    }

    #[test]
    fn releases_extension_and_reuses_unchanged_version() {
        let root = temp_root("release");
        let source = root.join("source");
        let flavor = source.join(CHROMIUM_FLAVOR);
        fs::create_dir_all(flavor.join("chunks")).unwrap();
        fs::write(flavor.join("manifest.json"), r#"{"version":"0.1.0"}"#).unwrap();
        fs::write(flavor.join("chunks").join("a.js"), "a").unwrap();

        let target_root = root.join("target");
        let first = release_flavor(&source, CHROMIUM_FLAVOR, &target_root).unwrap();
        assert!(first.join("chunks").join("a.js").is_file());
        assert_eq!(fs::read_to_string(first.join(STAMP_FILE)).unwrap(), "0.1.0");

        // 版本没变就复用现成目录，不重复复制
        fs::write(first.join("local-marker"), "keep").unwrap();
        let second = release_flavor(&source, CHROMIUM_FLAVOR, &target_root).unwrap();
        assert!(second.join("local-marker").is_file());

        // 版本变化时整体重建，旧文件不残留
        fs::write(flavor.join("manifest.json"), r#"{"version":"0.2.0"}"#).unwrap();
        let third = release_flavor(&source, CHROMIUM_FLAVOR, &target_root).unwrap();
        assert_eq!(fs::read_to_string(third.join(STAMP_FILE)).unwrap(), "0.2.0");
        assert!(!third.join("local-marker").exists());

        fs::remove_dir_all(&root).ok();
    }
}