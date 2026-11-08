use std::path::PathBuf;

use tauri::{AppHandle, Manager};

pub fn data_dir(app: &AppHandle) -> PathBuf {
    if let Some(value) = std::env::var_os("PAPER_REFLOW_DATA") {
        return PathBuf::from(value);
    }
    if let Some(value) = std::env::var_os("LOCALAPPDATA") {
        return PathBuf::from(value).join("paper-reflow");
    }
    app.path()
        .app_local_data_dir()
        .map(|path| path.join("paper-reflow"))
        .unwrap_or_else(|_| PathBuf::from("paper-reflow"))
}

pub fn ensure_data_dir(app: &AppHandle) -> PathBuf {
    let dir = data_dir(app);
    for child in ["papers", "logs", "backups"] {
        let _ = std::fs::create_dir_all(dir.join(child));
    }
    dir
}

pub fn sidecar_path(app: &AppHandle) -> Result<PathBuf, String> {
    if let Ok(value) = std::env::var("PAPER_REFLOW_SIDECAR") {
        let path = PathBuf::from(value);
        if path.is_file() {
            return Ok(path);
        }
    }

    if let Ok(resource) = app.path().resource_dir() {
        for base in [resource.clone(), resource.join("resources")] {
            for candidate in [
                base.join("paper-reflow-server")
                    .join("paper-reflow-server.exe"),
                base.join("paper-reflow-server.exe"),
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
            .join("../dist-sidecar/paper-reflow-server/paper-reflow-server.exe");
        if dev.is_file() {
            return Ok(dev);
        }
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            for candidate in [
                parent.join("paper-reflow-server.exe"),
                parent
                    .join("paper-reflow-server")
                    .join("paper-reflow-server.exe"),
            ] {
                if candidate.is_file() {
                    return Ok(candidate);
                }
            }
        }
    }

    Err("找不到 paper-reflow-server.exe，请先运行 npm run stage:sidecar".into())
}

pub fn log_line(app: &AppHandle, message: &str) {
    use std::io::Write;

    let dir = data_dir(app).join("logs");
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
