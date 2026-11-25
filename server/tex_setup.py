"""LaTeX 引擎（MiKTeX 便携版）的后台安装。

为什么不走 winget
-----------------
设置页原来的「一键安装 MiKTeX」是 ``winget install --id MiKTeX.MiKTeX``，
线上反馈的失败形态几乎都出在这条链上：

1. winget 安装 MiKTeX 需要管理员权限，UAC 被拒或没有交互桌面时直接失败；
2. winget 与 MiKTeX 安装器都会用系统 ``TEMP`` / 下载目录，这两处被重定向到
   不可写位置时安装器只报一句 ``Windows error``；
3. 装完不刷新进程 PATH，应用内仍显示「未检测到」，必须重启应用。

这里改成完全自有的安装链路：从 CTAN 镜像下载官方 ``miktexsetup``，用
``--portable`` 装到应用数据目录 ``<数据目录>/engines/miktex``。

- 不写注册表、不改 PATH、不需要管理员；
- 全程把 ``TEMP``/``TMP`` 指到应用数据目录，避开系统临时目录不可写；
- 给子进程一个干净的 PATH——用户的 PATH 里常有非法条目（把 exe 当目录写进去），
  MiKTeX 包管理器遍历时会直接报错；
- 装好后 ``latex.py`` 按固定路径探测，**立即生效、无需重启**；
- 配置镜像 + ``AutoInstall=1``，之后导出时缺什么宏包自动下载（含中文 ctex）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from pathlib import Path
from typing import Any

from server.winproc import hidden_kwargs, popen_hidden

# miktexsetup 是官方无人值守安装工具，版本固定，包集从镜像现场下载。
SETUP_VERSION = "5.5.0"
SETUP_ZIP = f"miktexsetup-{SETUP_VERSION}+1763023-x64.zip"
SETUP_EXE = "miktexsetup_standalone.exe"
PACKAGE_SET = "basic"
# basic 包集实测约 251MB；用于把下载进度换算成百分比（超过即封顶）。
TOTAL_BYTES = 260 * 1024 * 1024
# 中文模板（ctexart）依赖，basic 包集不含；装好后立即补上，避免第一次导出才等。
EXTRAS = ("ctex",)
CHUNK = 256 * 1024
MAX_LOG = 60

_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_SCHEDULED = False
_LOG: deque[str] = deque(maxlen=MAX_LOG)
_VERSION: str | None = None
_STATE: dict[str, Any] = {
    "status": "idle",  # idle | running | done | error
    "phase": "",  # download | packages | install | configure | extras | done
    "percent": 0,
    "error": "",
    "warning": "",
    "startedAt": 0,
    "finishedAt": 0,
}


def install_root() -> Path:
    """安装根目录：``<数据目录>/engines/miktex``。"""
    from server.runtime import default_data_dir

    return default_data_dir() / "engines" / "miktex"


def install_dir() -> Path:
    """便携版落点：``<数据目录>/engines/miktex/portable``。"""
    return install_root() / "portable"


def bin_dir() -> Path:
    """MiKTeX 可执行文件目录（便携版固定结构）。"""
    return install_dir() / "texmfs" / "install" / "miktex" / "bin" / "x64"


def installed_exe() -> Path | None:
    target = bin_dir() / "xelatex.exe"
    return target if target.is_file() else None


def _cache_dir() -> Path:
    return install_root() / "cache"


def _tmp_dir() -> Path:
    return install_root() / "tmp"


def _staging_dir() -> Path:
    return install_root() / "staging"


def _mirror_bases() -> list[str]:
    """CTAN 镜像（按国内可达性排序）。"""
    override = os.environ.get("PAPEREAD_TEX_MIRROR", "").strip()
    if override:
        return [override.rstrip("/")]
    return [
        "https://mirrors.tuna.tsinghua.edu.cn/CTAN/systems/win32/miktex",
        "https://mirrors.ustc.edu.cn/CTAN/systems/win32/miktex",
        "https://mirrors.aliyun.com/CTAN/systems/win32/miktex",
        "https://mirror.ctan.org/systems/win32/miktex",
    ]


def _setup_urls() -> list[str]:
    override = os.environ.get("PAPEREAD_TEX_SETUP_URL", "").strip()
    if override:
        return [override]
    quoted = urllib.parse.quote(SETUP_ZIP)
    return [f"{base}/setup/windows-x64/{quoted}" for base in _mirror_bases()]


def _repository_urls() -> list[str]:
    override = os.environ.get("PAPEREAD_TEX_REPOSITORY", "").strip()
    if override:
        return [override.rstrip("/") + "/"]
    return [f"{base}/tm/packages/" for base in _mirror_bases()]


def _clean_env() -> dict[str, str]:
    """子进程环境：可写临时目录 + 干净 PATH。

    MiKTeX 会遍历 PATH 建立链接/文件数据库，PATH 里有不存在的目录或把 exe
    当目录写进去（本机实测 ``D:\\jdk\\bin\\java.exe``）都会让它报 Windows error。
    """
    env = dict(os.environ)
    tmp = _tmp_dir()
    tmp.mkdir(parents=True, exist_ok=True)
    env["TEMP"] = str(tmp)
    env["TMP"] = str(tmp)
    system = os.environ.get("SystemRoot") or r"C:\Windows"
    parts = [
        str(Path(system) / "system32"),
        system,
        str(Path(system) / "System32" / "Wbem"),
        str(bin_dir()),
    ]
    env["PATH"] = ";".join(dict.fromkeys(parts))
    return env


def _set(**fields: Any) -> None:
    with _LOCK:
        _STATE.update(fields)


def _log(line: str) -> None:
    text = (line or "").strip()
    if text:
        _LOG.append(text)


def _log_tail(limit: int = 6) -> str:
    return " | ".join(list(_LOG)[-limit:])


def _probe_version(exe: Path) -> str:
    global _VERSION
    if _VERSION is not None:
        return _VERSION
    try:
        proc = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            timeout=20,
            env=_clean_env(),
            **hidden_kwargs(),
        )
        raw = (proc.stdout or proc.stderr).decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"\(MiKTeX ([0-9][0-9A-Za-z._-]*)", raw)
    _VERSION = match.group(1) if match else ""
    return _VERSION


def status() -> dict[str, Any]:
    exe = installed_exe()
    with _LOCK:
        data = dict(_STATE)
    if exe is not None and data["status"] == "idle":
        # 应用重启后状态机是新的，但目录还在：按已完成返回，前端才不会显示「未开始」。
        data["status"] = "done"
        data["phase"] = "done"
        data["percent"] = 100
    data["installed"] = exe is not None
    data["version"] = _probe_version(exe) if exe is not None else ""
    data["bin"] = str(bin_dir()) if exe is not None else ""
    return data


def _download_setup(target: Path) -> None:
    errors: list[str] = []
    for url in _setup_urls():
        try:
            _set(phase="download", percent=4, error="")
            request = urllib.request.Request(url, headers={"User-Agent": "paperead"})
            with urllib.request.urlopen(request, timeout=60) as response:
                content_type = (response.headers.get("Content-Type") or "").lower()
                if "html" in content_type or content_type.startswith("application/json"):
                    raise ValueError("镜像返回网页而不是安装包")
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                with target.open("wb") as handle:
                    while True:
                        block = response.read(CHUNK)
                        if not block:
                            break
                        handle.write(block)
                        done += len(block)
                        if total:
                            _set(percent=min(8, 4 + int(done * 4 / total)))
            with zipfile.ZipFile(target) as bundle:
                if SETUP_EXE not in bundle.namelist():
                    raise ValueError("安装包里没有 miktexsetup")
            return
        except (OSError, ValueError, zipfile.BadZipFile, urllib.error.URLError) as error:
            host = url.split("/")[2] if "://" in url else url
            errors.append(f"{host}: {error}")
            target.unlink(missing_ok=True)
    raise RuntimeError("安装程序下载失败：" + "；".join(errors)[-500:])


def _extract_setup(archive: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle, bundle.open(SETUP_EXE) as source, target.open(
        "wb"
    ) as handle:
        shutil.copyfileobj(source, handle)


def _cache_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except OSError:
        return total
    return total


def _spawn(args: list[str]) -> subprocess.Popen[str]:
    proc = popen_hidden(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=_clean_env(),
    )
    assert proc.stdout is not None

    def pump() -> None:
        for line in proc.stdout:
            _log(line)

    threading.Thread(target=pump, daemon=True).start()
    return proc


def _wait(
    proc: subprocess.Popen[str],
    *,
    timeout: int,
    on_tick=None,
) -> int:
    started = time.time()
    while proc.poll() is None:
        if time.time() - started > timeout:
            proc.kill()
            raise RuntimeError("安装超时，请检查网络后重试")
        if on_tick is not None:
            on_tick()
        time.sleep(1.5)
    return proc.wait()


def _run_setup(setup: Path, phase: str, timeout: int, on_tick=None) -> int:
    # miktexsetup 只有 download / install 两个任务；「packages」是给界面看的阶段名。
    action = "download" if phase == "packages" else phase
    command = [
        str(setup),
        f"--portable={_staging_dir()}",
        f"--local-package-repository={_cache_dir()}",
        f"--package-set={PACKAGE_SET}",
        "--modify-path=no",
    ]
    repository = _repository_urls()[0]
    if phase == "packages":
        command.append(f"--remote-package-repository={repository}")
    command.append(action)
    _log("$ miktexsetup " + action)
    proc = _spawn(command)
    return _wait(proc, timeout=timeout, on_tick=on_tick)


def _run_exe(*args: str, timeout: int = 180) -> int:
    """跑 MiKTeX 自带工具（initexmf / miktex），返回退出码。"""
    command = [str(bin_dir() / args[0]), *args[1:]]
    _log("$ " + " ".join(command))
    proc = _spawn(command)
    return _wait(proc, timeout=timeout)


def _configure(repository: str) -> None:
    """固定镜像 + 打开静默自动装包，之后编译缺宏包时直接下载。"""
    _run_exe(
        "initexmf.exe",
        f"--set-config-value=[MPM]RemoteRepository={repository}",
        timeout=120,
    )
    _run_exe("initexmf.exe", "--set-config-value=[MPM]AutoInstall=1", timeout=120)


def _install_extras() -> str:
    """补装中文支持；失败不致命（编译时 AutoInstall 仍会补）。"""
    failed: list[str] = []
    for name in EXTRAS:
        try:
            code = _run_exe("miktex.exe", "packages", "install", name, timeout=900)
            if code != 0:
                failed.append(name)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            failed.append(name)
    return "；".join(failed)


def _promote() -> None:
    """staging 安装成功后整体替换旧目录。"""
    staging = _staging_dir()
    target = install_dir()
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, target)


def _register() -> None:
    """把引擎目录加进当前进程 PATH，让 ``shutil.which`` 也能立即命中。"""
    exe = installed_exe()
    if exe is None:
        return
    bin_path = str(exe.parent)
    current = os.environ.get("PATH", "")
    parts = [p for p in current.split(";") if p]
    if bin_path not in parts:
        os.environ["PATH"] = bin_path + (";" + current if current else "")


def _invalidate_toolchain_cache() -> None:
    try:
        from server.toolchain_api import invalidate_cache

        invalidate_cache()
    except Exception:  # noqa: BLE001 - 缓存失效失败不影响安装结果
        pass


def _ensure_space() -> None:
    """安装后约 480MB，加上下载缓存需要约 1GB；空间不足时早点给出可读提示。"""
    try:
        free = shutil.disk_usage(install_root()).free
    except OSError:
        return
    if free < 1024 * 1024 * 1024:
        raise RuntimeError(
            f"磁盘空间不足（剩余 {free / (1024 ** 3):.1f}GB），安装 LaTeX 引擎约需 1GB。"
        )


def _run() -> None:
    global _THREAD
    root = install_root()
    root.mkdir(parents=True, exist_ok=True)
    staging = _staging_dir()
    shutil.rmtree(staging, ignore_errors=True)
    archive = root / "miktexsetup.zip"
    setup = root / SETUP_EXE
    try:
        _ensure_space()
        _set(status="running", phase="download", percent=0, error="", warning="")
        _download_setup(archive)
        _extract_setup(archive, setup)
        _set(phase="packages", percent=8)
        repository = _repository_urls()[0]

        def packages_tick() -> None:
            size = _cache_size(_cache_dir())
            _set(percent=min(72, 8 + int(size / TOTAL_BYTES * 64)))

        try:
            code = _run_setup(setup, "packages", timeout=2400, on_tick=packages_tick)
            if code != 0:
                raise RuntimeError(f"宏包下载失败（退出码 {code}）：{_log_tail()}")
            _set(phase="install", percent=74)
            code = _run_setup(setup, "install", timeout=1800)
            if code != 0:
                raise RuntimeError(f"引擎安装失败（退出码 {code}）：{_log_tail()}")
            _promote()
            _set(phase="configure", percent=88)
            _configure(repository)
            _set(phase="extras", percent=92)
            warning = _install_extras()
            _register()
            _set(
                status="done",
                phase="done",
                percent=100,
                warning=("中文支持预装失败，首次导出时会自动补装：" + warning) if warning else "",
                finishedAt=int(time.time()),
            )
            _invalidate_toolchain_cache()
            # 清理失败（文件被占用等）不能反过来把已完成的安装标成失败。
            shutil.rmtree(_cache_dir(), ignore_errors=True)
            for leftover in (archive, setup):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass
        except RuntimeError:
            raise
        except Exception as error:  # noqa: BLE001 - 统一回显
            raise RuntimeError(f"安装失败：{error}") from error
    except Exception as error:  # noqa: BLE001 - 安装失败要回显给用户
        shutil.rmtree(staging, ignore_errors=True)
        _set(
            status="error",
            phase="",
            error=str(error)[:800],
            finishedAt=int(time.time()),
        )
    finally:
        with _LOCK:
            _THREAD = None


def start() -> dict[str, Any]:
    """开始后台安装；已装好或正在安装时直接返回当前状态。"""
    global _THREAD
    with _LOCK:
        if installed_exe() is not None:
            _register()
            _STATE.update(status="done", phase="done", percent=100)
            return status()
        if _THREAD is not None and _THREAD.is_alive():
            return status()
        if os.name != "nt":
            _STATE.update(
                status="error",
                error="当前平台请通过系统包管理器安装 TeX Live 或 MiKTeX。",
                finishedAt=int(time.time()),
            )
            return status()
        _LOG.clear()
        _STATE.update(
            status="running", phase="download", percent=0, error="", warning="",
            startedAt=int(time.time()), finishedAt=0,
        )
        _THREAD = threading.Thread(target=_run, daemon=True, name="tex-install")
        _THREAD.start()
    return status()


def start_if_missing() -> None:
    """应用启动时调用：本机没有 TeX 引擎就后台补齐（不阻塞启动）。"""
    global _SCHEDULED
    if os.environ.get("PAPEREAD_MOCK") == "1":
        return
    if os.environ.get("PAPEREAD_TEX_AUTODOWNLOAD", "1") == "0":
        return
    if os.name != "nt":
        return
    if installed_exe() is not None:
        _register()
        _set(status="done", phase="done", percent=100)
        return
    try:
        from server.research.latex import find_engines

        # 系统里已经装过 TeX（TeX Live / MiKTeX）就不要再下一个 250MB。
        if find_engines():
            return
    except Exception:  # noqa: BLE001 - 探测失败时按缺失处理
        pass
    if status()["status"] == "running" or _SCHEDULED:
        return
    # 与 pandoc / node / 引擎的启动下载错开，避免首启同时抢带宽。
    delay = float(os.environ.get("PAPEREAD_TEX_AUTODOWNLOAD_DELAY", "20") or 0)
    _SCHEDULED = True

    def fire() -> None:
        global _SCHEDULED
        _SCHEDULED = False
        try:
            start()
        except OSError:
            pass

    timer = threading.Timer(max(0.0, delay), fire)
    timer.daemon = True
    timer.start()


def reset_for_tests() -> None:
    """测试用：把状态机恢复成初始值。"""
    global _THREAD, _SCHEDULED, _VERSION
    with _LOCK:
        _THREAD = None
        _SCHEDULED = False
        _VERSION = None
        _LOG.clear()
        _STATE.update(
            status="idle", phase="", percent=0, error="", warning="",
            startedAt=0, finishedAt=0,
        )