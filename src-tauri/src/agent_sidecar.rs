use std::fs;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use rand::RngCore;
use tauri::{AppHandle, Manager};

use crate::desktop::{api, paths};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

const CREATE_NO_WINDOW: u32 = 0x0800_0000;

pub struct AgentSidecar {
    pub child: Child,
    pub token: String,
    #[cfg(windows)]
    job: Option<isize>,
}

impl AgentSidecar {
    /// The endpoint is reserved by the caller (`main`) before the Python sidecar
    /// starts, so the proxy can be configured through the environment.
    pub fn start(
        app: &AppHandle,
        python_port: u16,
        python_token: &str,
        data_dir: &Path,
        port: u16,
        token: String,
    ) -> Result<Self, String> {
        let agent_dir = agent_dir(app)?;
        let bin = agent_dir.join("src").join("bin.mjs");
        if !bin.is_file() {
            return Err(format!(
                "找不到 agent 启动文件：{}。请先在 agent/ 下执行 npm install。",
                bin.display()
            ));
        }

        let agent_home = agent_home(app, data_dir);
        ensure_profile(&agent_dir, &agent_home)?;
        let tmp_dir = data_dir.join("tmp");
        let _ = fs::create_dir_all(&tmp_dir);
        let log_dir = data_dir.join("logs");
        let _ = fs::create_dir_all(&log_dir);
        let log_path = log_dir.join("agent.log");
        let stdout = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .map_err(|error| format!("打开 agent 日志失败：{error}"))?;
        let stderr = stdout
            .try_clone()
            .map_err(|error| format!("复制 agent 日志句柄失败：{error}"))?;

        let node = wait_for_node(app, &agent_dir, data_dir, python_port, python_token)?;
        let env_key = paths::var("PAPEREAD_LLM_KEY")
            .or_else(|| paths::var("PAPEREAD_API_KEY"))
            .or_else(|| std::env::var("DEEPSEEK_API_KEY").ok())
            .unwrap_or_default();
        let env_base = paths::var("PAPEREAD_LLM_BASE").or_else(|| paths::var("PAPEREAD_BASE_URL"));
        // 只有显式设置 PAPEREAD_LLM_DIRECT=1 才允许绕过本机代理（调试用）。
        // 机器上残留的 PAPEREAD_LLM_BASE 会让研究 agent 跳过设置页里的统一模型
        // 配置，直接连一个旧地址，表现为「阅读/翻译正常、研究/综述一直失败」。
        let direct_base = if paths::var("PAPEREAD_LLM_DIRECT").is_some() {
            env_base
        } else {
            None
        };
        // 默认走本机 LLM 代理，这样研究输入框里切换模型可以即时生效。
        let (llm_base, llm_key) = match direct_base {
            Some(base) => (base, env_key),
            None => (
                format!("http://127.0.0.1:{python_port}/api/llm-proxy/v1"),
                python_token.to_string(),
            ),
        };
        let model = paths::var("PAPEREAD_MODEL")
            .unwrap_or_else(|| "deepseek-v4-flash".into());

        paths::log_line(
            app,
            &format!(
                "spawn agent node={} bin={} port={} home={} log={}",
                node.display(),
                bin.display(),
                port,
                agent_home.display(),
                log_path.display()
            ),
        );

        // node 所在目录放到 PATH 最前面（安装版是首启下载的运行时目录），
        // 研究 agent 的 pwsh/bash 才能直接调用 node 运行脚本。
        let node_dir = node
            .parent()
            .map(|dir| dir.display().to_string())
            .unwrap_or_else(|| agent_dir.display().to_string());
        let path_sep = if cfg!(windows) { ";" } else { ":" };
        let inherited_path = std::env::var("PATH").unwrap_or_default();
        let shell_path = if inherited_path.is_empty() {
            node_dir
        } else {
            format!("{}{}{}", node_dir, path_sep, inherited_path)
        };
        let python = detect_python();

        let mut command = Command::new(&node);
        command
            .arg(&bin)
            .current_dir(&agent_dir)
            .env("DSH_HOME", &agent_home)
            .env("PAPEREAD_AGENT_PORT", port.to_string())
            .env("PAPEREAD_AGENT_TOKEN", &token)
            .env("PAPEREAD_AGENT_COMPOSE", "full")
            .env("PAPEREAD_API_BASE", format!("http://127.0.0.1:{python_port}"))
            .env("PATH", shell_path)
            .env("TMP", &tmp_dir)
            .env("TEMP", &tmp_dir)
            .env("TMPDIR", &tmp_dir)
            .env("PAPEREAD_TOKEN", python_token)
            .env("PAPEREAD_INTERNAL_TOKEN", python_token)
            .env("PAPEREAD_LLM_KEY", llm_key)
            .env("PAPEREAD_LLM_BASE", llm_base)
            .env("PAPEREAD_MODEL", model)
            .env("PAPEREAD_PYTHON", python.unwrap_or_default())
            .stdin(Stdio::null())
            .stdout(Stdio::from(stdout))
            .stderr(Stdio::from(stderr));
        #[cfg(windows)]
        command.creation_flags(CREATE_NO_WINDOW);

        let child = command
            .spawn()
            .map_err(|error| format!("启动研究 agent 失败：{error}"))?;
        let job = assign_job(&child);
        let mut sidecar = Self {
            child,
            token,
            #[cfg(windows)]
            job,
        };

        let deadline = Instant::now() + Duration::from_secs(30);
        loop {
            let last_error = match api::agent_health(port, &sidecar.token) {
                Ok(value) if value["ok"].as_bool() == Some(true) => {
                    paths::log_line(app, "agent ready");
                    return Ok(sidecar);
                }
                Ok(value) => format!("health 异常：{value}"),
                Err(error) => error,
            };
            if Instant::now() >= deadline {
                sidecar.stop();
                return Err(format!(
                    "研究 agent 启动超时：{last_error}\n日志：{}",
                    log_path.display()
                ));
            }
            // 100ms 一探：agent 已在后台，缩短探测间隔让它更早可用。
            std::thread::sleep(Duration::from_millis(100));
        }
    }

    pub fn stop(&mut self) {
        #[cfg(windows)]
        if let Some(raw) = self.job.take() {
            unsafe {
                let _ = windows::Win32::Foundation::CloseHandle(
                    windows::Win32::Foundation::HANDLE(raw as *mut core::ffi::c_void),
                );
            }
        }
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

impl Drop for AgentSidecar {
    fn drop(&mut self) {
        self.stop();
    }
}

pub fn free_port() -> Result<u16, String> {
    let listener =
        TcpListener::bind("127.0.0.1:0").map_err(|error| format!("找不到空闲端口：{error}"))?;
    let port = listener
        .local_addr()
        .map_err(|error| format!("读取端口失败：{error}"))?
        .port();
    drop(listener);
    Ok(port)
}

pub fn random_token() -> String {
    let mut bytes = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut bytes);
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn agent_dir(app: &AppHandle) -> Result<PathBuf, String> {
    if let Some(value) = paths::var("PAPEREAD_AGENT_DIR") {
        let path = PathBuf::from(value);
        if path.join("node_modules").is_dir() {
            return Ok(plain_path(path));
        }
    }

    if let Ok(resource) = app.path().resource_dir() {
        for base in [resource.clone(), resource.join("resources")] {
            let candidate = base.join("agent");
            if candidate.join("node_modules").is_dir() {
                return Ok(plain_path(candidate));
            }
        }
    }

    #[cfg(debug_assertions)]
    {
        let dev = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../agent");
        if dev.join("node_modules").is_dir() {
            return Ok(dev);
        }
    }

    Err("找不到 agent/（先运行 npm install --prefix agent）".into())
}

/// Tauri 的 `resource_dir()` 在 Windows 上返回 `\\?\C:\...` 形式的长路径；
/// Node 解析这个前缀时会把 `C:` 当成目录并抛 EISDIR，导致 agent 起不来。
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

/// 探测可用的 Python 解释器；没有就返回空串，让 agent 明确告知用户
/// 而不是反复尝试一个不存在的命令。优先官方启动器 `py -3`（Windows），
/// 再退回 PATH 上的 `python` / `python3`。
fn detect_python() -> Option<String> {
    for candidate in ["py", "python", "python3"] {
        let mut command = Command::new(candidate);
        if candidate == "py" {
            command.arg("-3");
        }
        command.arg("--version");
        #[cfg(windows)]
        command.creation_flags(CREATE_NO_WINDOW);
        let ok = command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|status| status.success())
            .unwrap_or(false);
        if ok {
            return Some(if candidate == "py" {
                "py -3".to_string()
            } else {
                candidate.to_string()
            });
        }
    }
    None
}

/// node 运行时查找顺序：显式 `PAPEREAD_NODE` → 安装包内置（旧版升级残留）
/// → 首启下载目录 `<数据目录>/engines/node/<版本>/node.exe` → 系统 PATH。
///
/// 安装包不再内置约 86 MB 的 node.exe（会顶破 Gitee 100 MB 附件上限），
/// agent 与公式引擎共用首启后台下载的同一份运行时。
fn find_node(agent_dir: &Path, data_dir: &Path) -> Option<PathBuf> {
    if let Some(value) = paths::var("PAPEREAD_NODE") {
        let path = PathBuf::from(value);
        if path.is_file() {
            return Some(path);
        }
    }
    let bundled = agent_dir.join("node.exe");
    if bundled.is_file() {
        return Some(bundled);
    }
    let root = data_dir.join("engines").join("node");
    if let Ok(entries) = fs::read_dir(&root) {
        for entry in entries.flatten() {
            let candidate = entry.path().join("node.exe");
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    if let Ok(value) = std::env::var("PATH") {
        for dir in value.split(';').filter(|dir| !dir.is_empty()) {
            let candidate = PathBuf::from(dir).join("node.exe");
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    None
}

/// 等 node 运行时就绪（首启下载中）。agent 起不来时研究/综述会返回 503，
/// 前端会自动退避重试，因此这里可以安全等待，不必让窗口卡在启动页。
fn wait_for_node(
    app: &AppHandle,
    agent_dir: &Path,
    data_dir: &Path,
    python_port: u16,
    python_token: &str,
) -> Result<PathBuf, String> {
    if let Some(path) = find_node(agent_dir, data_dir) {
        return Ok(path);
    }
    if paths::var("PAPEREAD_NODE_AUTODOWNLOAD").as_deref() == Some("0") {
        return Err("未找到 node 运行时，且自动下载已关闭（PAPEREAD_NODE_AUTODOWNLOAD=0）。".into());
    }
    // 触发一次后台下载（Python 已就绪；已在下载时接口幂等返回当前状态）。
    let url = format!("http://127.0.0.1:{python_port}/api/toolchain/node");
    match ureq::post(&url)
        .set("X-Paperead-Token", python_token)
        .timeout(Duration::from_secs(5))
        .call()
    {
        Ok(_) => paths::log_line(app, "node 运行时未安装：已触发后台下载，等待就绪"),
        Err(error) => paths::log_line(
            app,
            &format!("触发 node 运行时下载失败（继续等待已有下载）：{error}"),
        ),
    }
    let deadline = Instant::now() + Duration::from_secs(480);
    while Instant::now() < deadline {
        std::thread::sleep(Duration::from_secs(2));
        if let Some(path) = find_node(agent_dir, data_dir) {
            paths::log_line(app, &format!("node 运行时已就绪：{}", path.display()));
            return Ok(path);
        }
    }
    Err("node 运行时尚未下载完成（首次启动约 33 MB，通常 1–3 分钟）。请稍后重启应用，或在「设置 → 工具链」里重试。".into())
}

fn agent_home(_app: &AppHandle, data_dir: &Path) -> PathBuf {
    if let Some(value) = paths::var("PAPEREAD_AGENT_HOME") {
        return PathBuf::from(value);
    }
    data_dir.join("dsh")
}

fn ensure_profile(agent_dir: &Path, agent_home: &Path) -> Result<(), String> {
    let source = agent_dir.join("profiles").join("research");
    let target = agent_home.join("profiles").join("research");
    if target.join("package.json").is_file() {
        return Ok(());
    }
    let _ = fs::create_dir_all(&target);
    copy_dir(&source, &target)
        .map_err(|error| format!("复制 research profile 失败：{error}"))
}

fn copy_dir(source: &Path, target: &Path) -> std::io::Result<()> {
    fs::create_dir_all(target)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let path = entry.path();
        let dest = target.join(entry.file_name());
        if path.is_dir() {
            copy_dir(&path, &dest)?;
        } else {
            fs::copy(&path, &dest)?;
        }
    }
    Ok(())
}

#[cfg(windows)]
fn assign_job(child: &Child) -> Option<isize> {
    use std::os::windows::io::AsRawHandle;

    use windows::Win32::Foundation::{CloseHandle, HANDLE};
    use windows::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    unsafe {
        let job = CreateJobObjectW(None, None).ok()?;
        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
        .is_err()
        {
            let _ = CloseHandle(job);
            return None;
        }
        let process = HANDLE(child.as_raw_handle() as *mut core::ffi::c_void);
        if AssignProcessToJobObject(job, process).is_err() {
            let _ = CloseHandle(job);
            return None;
        }
        Some(job.0 as isize)
    }
}

#[cfg(not(windows))]
fn assign_job(_child: &Child) -> Option<isize> {
    None
}
