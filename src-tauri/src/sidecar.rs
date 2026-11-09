use std::fs::OpenOptions;
use std::net::TcpListener;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use rand::RngCore;
use tauri::AppHandle;

use crate::desktop::{api, paths};
#[cfg(windows)]
use std::os::windows::io::AsRawHandle;
#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
use windows::Win32::Foundation::{CloseHandle, HANDLE};
#[cfg(windows)]
use windows::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};

const CREATE_NO_WINDOW: u32 = 0x0800_0000;

pub struct Sidecar {
    pub child: Child,
    pub port: u16,
    pub token: String,
    #[cfg(windows)]
    job: Option<isize>,
}

impl Sidecar {
    /// `agent` is the already-reserved research-agent endpoint. It is injected
    /// into the Python process environment so the proxy can reach the agent
    /// without a handshake file on disk.
    pub fn start(app: &AppHandle, agent: Option<(u16, String)>) -> Result<Self, String> {
        let data_dir = paths::ensure_data_dir(app);
        let port = free_port()?;
        let token = random_token();
        let executable = paths::sidecar_path(app)?;

        paths::log_line(
            app,
            &format!(
                "spawn sidecar={} port={} data={}",
                executable.display(),
                port,
                data_dir.display()
            ),
        );

        let mut command = Command::new(&executable);
        let stdout = OpenOptions::new()
            .create(true)
            .append(true)
            .open(data_dir.join("logs").join("server.log"))
            .map_err(|error| format!("打开 sidecar 日志失败：{error}"))?;
        let stderr = stdout
            .try_clone()
            .map_err(|error| format!("复制 sidecar 日志句柄失败：{error}"))?;
        command
            .env("PAPEREAD_PORT", port.to_string())
            .env("PAPEREAD_DATA", &data_dir)
            .env("PAPEREAD_TOKEN", &token)
            .env("PYTHONUTF8", "1")
            .stdin(Stdio::null())
            .stdout(Stdio::from(stdout))
            .stderr(Stdio::from(stderr));
        if let Some((agent_port, agent_token)) = agent {
            command
                .env("PAPEREAD_AGENT_PORT", agent_port.to_string())
                .env("PAPEREAD_AGENT_TOKEN", agent_token);
        }
        #[cfg(windows)]
        command.creation_flags(CREATE_NO_WINDOW);

        let child = command
            .spawn()
            .map_err(|error| format!("启动 sidecar 失败：{error}"))?;
        let job = assign_job(&child);
        let mut sidecar = Self {
            child,
            port,
            token,
            #[cfg(windows)]
            job,
        };

        let deadline = Instant::now() + Duration::from_secs(20);
        loop {
            let last_error = match api::health(port, &sidecar.token) {
                Ok(value) if value["ok"].as_bool() == Some(true) => {
                    paths::log_line(app, "sidecar ready");
                    return Ok(sidecar);
                }
                Ok(value) => format!("health 异常：{value}"),
                Err(error) => error,
            };
            if Instant::now() >= deadline {
                sidecar.stop();
                return Err(format!("sidecar 启动超时：{last_error}"));
            }
            std::thread::sleep(Duration::from_millis(200));
        }
    }

    pub fn stop(&mut self) {
        #[cfg(windows)]
        if let Some(raw) = self.job.take() {
            unsafe {
                let _ = CloseHandle(HANDLE(raw as *mut core::ffi::c_void));
            }
        }
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

impl Drop for Sidecar {
    fn drop(&mut self) {
        self.stop();
    }
}

fn free_port() -> Result<u16, String> {
    let listener =
        TcpListener::bind("127.0.0.1:0").map_err(|error| format!("找不到空闲端口：{error}"))?;
    let port = listener
        .local_addr()
        .map_err(|error| format!("读取端口失败：{error}"))?
        .port();
    drop(listener);
    Ok(port)
}

fn random_token() -> String {
    let mut bytes = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut bytes);
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(windows)]
fn assign_job(child: &Child) -> Option<isize> {
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
