use std::path::{Path, PathBuf};

use tauri::{AppHandle, Manager};

const DATA_DIR_NAME: &str = "paperead-data";
const LEGACY_DATA_DIR_NAME: &str = "paper-reflow-data";

/// 读环境变量：优先 `PAPEREAD_*`，回退旧版 `PAPER_REFLOW_*`。
pub fn var(name: &str) -> Option<String> {
    if let Ok(value) = std::env::var(name) {
        return Some(value);
    }
    let suffix = name.strip_prefix("PAPEREAD_")?;
    std::env::var(format!("PAPER_REFLOW_{suffix}")).ok()
}

fn var_os(name: &str) -> Option<std::ffi::OsString> {
    if let Some(value) = std::env::var_os(name) {
        return Some(value);
    }
    let suffix = name.strip_prefix("PAPEREAD_")?;
    std::env::var_os(format!("PAPER_REFLOW_{suffix}"))
}

fn read_location(file: &Path) -> Option<PathBuf> {
    let text = std::fs::read_to_string(file).ok()?;
    let value: serde_json::Value = serde_json::from_str(&text).ok()?;
    let path = value.get("path")?.as_str()?.trim();
    if path.is_empty() {
        return None;
    }
    Some(PathBuf::from(path))
}

fn configured_data_dir() -> Option<PathBuf> {
    // 设置页迁移文献库后写入 %APPDATA%\Paperead\data-location.json；
    // 主文件损坏时回退上一次备份，避免升级/异常退出后路径丢失。
    let base = std::env::var_os("APPDATA")?;
    let dir = PathBuf::from(base).join("Paperead");
    read_location(&dir.join("data-location.json"))
        .or_else(|| read_location(&dir.join("data-location.json.bak")))
}

fn library_has_data(dir: &Path) -> bool {
    dir.join("library.json").is_file()
        || dir.join("papers").is_dir()
        || dir.join("research").is_dir()
}

fn default_data_dir_path(app: &AppHandle) -> PathBuf {
    // 安装版把文献库放进安装目录下的 data：程序与数据在同一目录树，
    // 覆盖升级只会替换程序文件，不会动 data 子目录。
    #[cfg(all(desktop, not(debug_assertions)))]
    {
        if let Some(dir) = install_data_dir() {
            return dir;
        }
    }
    legacy_default_data_dir(app)
}

/// 历史默认目录：`%LOCALAPPDATA%\paperead-data`（旧版本安装版使用）。
fn legacy_default_data_dir(app: &AppHandle) -> PathBuf {
    if let Some(value) = std::env::var_os("LOCALAPPDATA") {
        return PathBuf::from(value).join(DATA_DIR_NAME);
    }
    app.path()
        .app_local_data_dir()
        .map(|path| path.join(DATA_DIR_NAME))
        .unwrap_or_else(|_| PathBuf::from(DATA_DIR_NAME))
}

/// 安装目录下的 data 子目录；开发态不启用，避免把数据写进 target。
#[cfg(all(desktop, not(debug_assertions)))]
fn install_data_dir() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    exe.parent().map(|dir| dir.join("data"))
}

/// 目录里是否有真实内容（忽略日志/临时目录和空骨架）。
#[cfg(all(desktop, not(debug_assertions)))]
fn dir_has_payload(dir: &Path) -> bool {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return false;
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if matches!(name.as_ref(), "logs" | "tmp" | "backups") {
            continue;
        }
        if entry.path().is_dir() {
            if dir_has_payload(&entry.path()) {
                return true;
            }
            continue;
        }
        return true;
    }
    false
}

/// 安装版首次启动：把历史默认库迁到安装目录 data。
/// 同盘直接改名，失败就继续用旧目录，保证数据与配置都不丢。
fn adopt_install_data(app: &AppHandle, target: PathBuf) -> PathBuf {
    #[cfg(all(desktop, not(debug_assertions)))]
    {
        let Some(install_data) = install_data_dir() else {
            return target;
        };
        if target != install_data {
            return target;
        }
        if dir_has_payload(&target) {
            return target;
        }
        let mut legacy = legacy_default_data_dir(app);
        if !dir_has_payload(&legacy) {
            let older = legacy.with_file_name(LEGACY_DATA_DIR_NAME);
            if dir_has_payload(&older) {
                legacy = older;
            }
        }
        if legacy == target || !dir_has_payload(&legacy) {
            return target;
        }
        // 目标只可能是上次启动留下的空骨架，清掉后再改名
        if target.exists() {
            let _ = std::fs::remove_dir_all(&target);
        }
        if let Some(parent) = target.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        match std::fs::rename(&legacy, &target) {
            Ok(()) => {
                eprintln!(
                    "[Paperead] migrated data dir {} -> {}",
                    legacy.display(),
                    target.display()
                );
                target
            }
            Err(error) => {
                eprintln!(
                    "[Paperead] migrate {} -> {} failed: {error}; keeping legacy dir",
                    legacy.display(),
                    target.display()
                );
                legacy
            }
        }
    }
    #[cfg(not(all(desktop, not(debug_assertions))))]
    {
        let _ = app;
        target
    }
}

pub fn data_dir(app: &AppHandle) -> PathBuf {
    if let Some(value) = var_os("PAPEREAD_DATA") {
        return PathBuf::from(value);
    }
    if let Some(path) = configured_data_dir() {
        // 自定义路径还在且有数据：继续用
        if library_has_data(&path) {
            return path;
        }
        // 自定义路径失效（移动硬盘未挂载、目录改名）：回退默认库，但保留配置
        let fallback = default_data_dir_path(app);
        if library_has_data(&fallback) {
            eprintln!(
                "[Paperead] configured data dir {} unavailable; using {}",
                path.display(),
                fallback.display()
            );
            return fallback;
        }
        return path;
    }
    default_data_dir_path(app)
}

/// 旧版数据目录改名为新目录；改名失败时返回旧目录继续使用。
fn adopt_legacy(dir: PathBuf) -> PathBuf {
    if dir.exists() {
        return dir;
    }
    let Some(parent) = dir.parent().map(|path| path.to_path_buf()) else {
        return dir;
    };
    let legacy = parent.join(LEGACY_DATA_DIR_NAME);
    if !legacy.is_dir() {
        return dir;
    }
    match std::fs::rename(&legacy, &dir) {
        Ok(()) => {
            eprintln!(
                "[Paperead] migrated data dir {} -> {}",
                legacy.display(),
                dir.display()
            );
            dir
        }
        Err(error) => {
            eprintln!(
                "[Paperead] migrate {} failed: {error}; keeping legacy dir",
                legacy.display()
            );
            legacy
        }
    }
}


fn prepare(dir: &std::path::Path) -> bool {
    if std::fs::create_dir_all(dir).is_err() {
        return false;
    }
    ["papers", "logs", "backups", "tmp"]
        .iter()
        .all(|child| std::fs::create_dir_all(dir.join(child)).is_ok())
}

pub fn ensure_data_dir(app: &AppHandle) -> PathBuf {
    let mut dir = data_dir(app);
    // 自定义路径生效时不再触发旧目录改名/只读回退；回退到默认库时仍要走 legacy 迁移
    let configured = configured_data_dir();
    let using_configured = configured.as_ref().is_some_and(|path| path == &dir);
    let explicit = var_os("PAPEREAD_DATA").is_some() || using_configured;
    if !explicit {
        dir = adopt_legacy(dir);
        dir = adopt_install_data(app, dir);
    }
    // A stale directory created by another Windows account can be read-only
    // for the current user. Prefer a sibling fallback over failing every write.
    if !explicit && !prepare(&dir) {
        let fallback = unwritable_fallback(app, &dir);
        if prepare(&fallback) {
            eprintln!(
                "[Paperead] data dir {} is not writable; using {}",
                dir.display(),
                fallback.display()
            );
            dir = fallback;
        }
    }
    for child in ["papers", "logs", "backups", "tmp"] {
        if let Err(error) = std::fs::create_dir_all(dir.join(child)) {
            eprintln!(
                "[Paperead] create data subdir {} failed: {error}",
                dir.join(child).display()
            );
        }
    }
    dir
}

/// 默认数据目录不可写时的备选：安装版回退到历史系统目录，开发态用同级目录。
fn unwritable_fallback(app: &AppHandle, dir: &Path) -> PathBuf {
    #[cfg(all(desktop, not(debug_assertions)))]
    {
        let legacy = legacy_default_data_dir(app);
        if legacy != dir {
            return legacy;
        }
    }
    let _ = app;
    dir.with_file_name("paperead-data-user")
}

pub fn sidecar_path(app: &AppHandle) -> Result<PathBuf, String> {
    if let Some(value) = var("PAPEREAD_SIDECAR") {
        let path = PathBuf::from(value);
        if path.is_file() {
            return Ok(path);
        }
    }

    if let Ok(resource) = app.path().resource_dir() {
        for base in [resource.clone(), resource.join("resources")] {
            for candidate in [
                base.join("paperead-server")
                    .join("paperead-server.exe"),
                base.join("paperead-server.exe"),
            ] {
                if candidate.is_file() {
                    return Ok(candidate);
                }
            }
        }
    }

    #[cfg(debug_assertions)]
    {
        let dev = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../dist-sidecar/paperead-server/paperead-server.exe");
        if dev.is_file() {
            return Ok(dev);
        }
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            for candidate in [
                parent.join("paperead-server.exe"),
                parent
                    .join("paperead-server")
                    .join("paperead-server.exe"),
            ] {
                if candidate.is_file() {
                    return Ok(candidate);
                }
            }
        }
    }

    Err("找不到 paperead-server.exe，请先运行 npm run stage:sidecar".into())
}

pub fn log_line(app: &AppHandle, message: &str) {
    use std::io::Write;

    let dir = ensure_data_dir(app).join("logs");
    let _ = std::fs::create_dir_all(&dir);
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("app.log"))
    {
        let _ = writeln!(file, "[{}] {message}", timestamp());
    }
}

fn timestamp() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default()
}
