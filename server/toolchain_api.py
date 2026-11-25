"""写作工具链自检与一键安装（设置页使用）。

安装包里只带 pandoc，PDF 编译需要本机的 TeX 引擎。这里提供三件事：

- ``GET /api/toolchain``：返回 pandoc / TeX 引擎 / bibtex 的检测结果；
- ``POST /api/toolchain/tex``：后台安装 MiKTeX 便携版（推荐，不需要管理员）；
- ``POST /api/toolchain/install``：用 winget 安装 MiKTeX（备用路径）。

winget 这条链对权限与环境敏感（UAC、系统 TEMP 不可写、装完 PATH 不刷新），
线上失败几乎都出在这里，所以默认走 ``tex_setup`` 的自有安装链路。
"""

from __future__ import annotations

import json
import os

import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from server import babeldoc_setup, node_setup, tex_setup
from server.research import pandoc_setup
from server.research.latex import find_engines, find_winget, toolchain_status
from server.winproc import popen_hidden

router = APIRouter(prefix="/api/toolchain", tags=["toolchain"])

_MAX_LOG_LINES = 400
_JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
_CURRENT: str | None = None

# 自检结果缓存：工具装好之后默认一直“已就绪”，不再每次打开设置页都探测一遍。
# 只有手动「重新检测」、安装完成，或导出时发现工具缺失才会作废重测。
_CACHE_NAME = "toolchain-status.json"
_CACHE: dict[str, Any] | None = None


def _cache_path() -> Path:
    from server.runtime import default_data_dir

    return default_data_dir() / _CACHE_NAME


def _load_cache() -> dict[str, Any] | None:
    """读取上次的自检结果；只有全部就绪才复用。

    缺工具时下次打开页面重新检测，避免用户在应用外装好 TeX 后一直显示未就绪。
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("ready"):
        return None
    _CACHE = data
    return _CACHE


def _save_cache(data: dict[str, Any]) -> None:
    global _CACHE
    _CACHE = data
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        # 写缓存失败不影响检测本身，下次重新探测即可。
        pass


def invalidate_cache() -> None:
    """工具缺失或安装完成后作废缓存，下次请求重新检测。"""
    global _CACHE
    _CACHE = None
    try:
        _cache_path().unlink(missing_ok=True)
    except OSError:
        pass


def _append(job: dict[str, Any], line: str) -> None:
    if not line:
        return
    with _LOCK:
        job["log"].append(line.rstrip())
        if len(job["log"]) > _MAX_LOG_LINES:
            del job["log"][: len(job["log"]) - _MAX_LOG_LINES]


def _install_env() -> dict[str, str]:
    """winget / 安装器的环境：临时目录必须可写，否则安装器只报 Windows error。"""
    from server.runtime import default_data_dir

    env = dict(os.environ)
    tmp = default_data_dir() / "tools" / "tmp"
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        env["TEMP"] = str(tmp)
        env["TMP"] = str(tmp)
    except OSError:
        pass
    return env


def _refresh_path() -> None:
    """把注册表里的最新 PATH 合并进当前进程。

    winget 装完只在注册表里更新 PATH，当前进程（sidecar）不会自动感知，
    于是设置页一直显示「未检测到」、必须重启应用——这就是原一键安装的第二类
    报错来源。这里主动读一次注册表，装完立即就能探测到。
    """
    if os.name != "nt":
        return
    try:
        import winreg

        locations = [
            (winreg.HKEY_CURRENT_USER, r"Environment"),
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            ),
        ]
        current = os.environ.get("PATH", "")
        parts = [item for item in current.split(";") if item]
        for hive, subkey in locations:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    raw, _ = winreg.QueryValueEx(key, "Path")
            except OSError:
                continue
            for item in str(raw).split(";"):
                item = item.strip()
                if item and item not in parts:
                    parts.append(item)
        os.environ["PATH"] = ";".join(parts)
    except Exception:  # noqa: BLE001 - 读注册表失败不影响后续探测
        pass


def _exit_hint(code: int) -> str:
    hints = {
        1603: "安装程序失败（1603），通常是管理员权限不足或被安全软件拦截。",
        1625: "系统策略禁止安装（1625），请改用自动安装。",
        3010: "安装成功，但系统要求重启后才完全生效。",
    }
    return hints.get(code, f"winget 退出码 {code}，请查看日志或改用自动安装。")


def _run_install(job_id: str) -> None:
    global _CURRENT
    job = _JOBS[job_id]
    winget = find_winget()
    if not winget:
        with _LOCK:
            job["status"] = "error"
            job["error"] = "没有找到 winget，请改用自动安装或从官网下载 MiKTeX 安装包。"
            job["finishedAt"] = int(time.time())
            _CURRENT = None
        return

    command = [
        winget,
        "install",
        "--id",
        "MiKTeX.MiKTeX",
        "-e",
        "--silent",
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--disable-interactivity",
    ]
    _append(job, "$ " + " ".join(command))
    try:
        proc = popen_hidden(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=_install_env(),
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            _append(job, line)
        code = proc.wait()
        with _LOCK:
            job["exitCode"] = code
        if code == 0 or code == 3010:
            # 安装器改的是注册表 PATH，当前进程刷新后才能立即探测到。
            _refresh_path()
            invalidate_cache()
            engines = find_engines()
            with _LOCK:
                if engines:
                    job["status"] = "done"
                else:
                    job["status"] = "error"
                    job["error"] = (
                        "安装程序已结束，但仍未检测到 TeX 引擎。"
                        "可能被安全软件拦截，建议改用自动安装。"
                    )
                job["finishedAt"] = int(time.time())
        else:
            with _LOCK:
                job["status"] = "error"
                job["error"] = _exit_hint(code) + " 日志：" + "\n".join(job["log"][-4:])
                job["finishedAt"] = int(time.time())
    except Exception as error:  # noqa: BLE001 - 安装失败要回显给用户
        with _LOCK:
            job["status"] = "error"
            job["error"] = str(error)
            job["finishedAt"] = int(time.time())
    finally:
        with _LOCK:
            _CURRENT = None


@router.get("")
def status(refresh: bool = Query(False)) -> dict[str, Any]:
    """默认返回缓存的自检结果；``?refresh=1`` 强制重新检测。"""
    download = pandoc_setup.status()
    tex = tex_setup.status()
    data = None if refresh else _load_cache()
    # 工具刚装好但缓存还是「未检测到」时补测一次：pandoc 下载完成、
    # 应用内 LaTeX 引擎安装完成都属于这种情况。
    stale_pandoc = download.get("installed") and not data.get("pandoc", {}).get("found")
    stale_tex = tex.get("installed") and not (data or {}).get("engines")
    if data is None or stale_pandoc or stale_tex:
        data = toolchain_status()
        _save_cache(data)
    return {
        **data,
        "install": {
            "running": _CURRENT is not None,
            "jobId": _CURRENT,
        },
        "pandocDownload": download,
        "nodeDownload": node_setup.status(),
        "engineDownload": babeldoc_setup.status(),
        "texDownload": tex,
    }


@router.get("/engine")
def engine_status() -> dict[str, Any]:
    """全局翻译引擎（BabelDOC）的安装状态与实时下载进度。

    与 PDF 导出链的 pandoc / TeX 检测分开暴露：翻译引擎是独立组件，
    未就绪时点「译文/对照」需要弹进度窗口，而不是只给一句报错。
    """
    return babeldoc_setup.status()


@router.post("/engine")
def engine_start() -> dict[str, Any]:
    """开始（或重试）后台下载翻译引擎；已就绪或正在下载时直接回状态。"""
    return babeldoc_setup.start()


@router.post("/pandoc")
def install_pandoc() -> dict[str, Any]:
    """安装包不带 pandoc：首次启动自动下载，这里也给一个手动重试入口。"""
    return pandoc_setup.start()


@router.post("/node")
def install_node() -> dict[str, Any]:
    """公式引擎所需的 node 运行时：首启后台下载，失败可在这里手动重试。"""
    return node_setup.start()


@router.post("/tex")
def install_tex() -> dict[str, Any]:
    """自动安装 LaTeX 引擎（MiKTeX 便携版）：无需管理员、装完立即生效。"""
    return tex_setup.start()


@router.post("/install")
def install() -> dict[str, Any]:
    global _CURRENT
    with _LOCK:
        if _CURRENT is not None:
            return {"jobId": _CURRENT, "status": "running"}
    if find_engines():
        return {"jobId": None, "status": "done", "notice": "已经检测到 LaTeX 引擎。"}

    job_id = uuid.uuid4().hex[:12]
    _JOBS[job_id] = {
        "id": job_id,
        "status": "running",
        "log": [],
        "exitCode": None,
        "error": None,
        "startedAt": int(time.time()),
        "finishedAt": None,
    }
    with _LOCK:
        _CURRENT = job_id
    threading.Thread(target=_run_install, args=(job_id,), daemon=True).start()
    return {"jobId": job_id, "status": "running"}


@router.get("/install/{job_id}")
def install_status(job_id: str) -> dict[str, Any]:
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="安装任务不存在")
    with _LOCK:
        return {**job, "log": list(job["log"])}
