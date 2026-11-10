"""写作工具链自检与一键安装（设置页使用）。

安装包里只带 pandoc，PDF 编译需要本机的 TeX 引擎。这里提供两件事：

- ``GET /api/toolchain``：返回 pandoc / TeX 引擎 / bibtex 的检测结果；
- ``POST /api/toolchain/install``：在后台用 winget 安装 MiKTeX，前端轮询进度。

安装是显式操作：不会在启动时自动装，也不会静默执行用户没点过的命令。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from server.research import pandoc_setup
from server.research.latex import find_winget, toolchain_status

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


def _run_install(job_id: str) -> None:
    global _CURRENT
    job = _JOBS[job_id]
    winget = find_winget()
    if not winget:
        with _LOCK:
            job["status"] = "error"
            job["error"] = "没有找到 winget，请从官网下载 MiKTeX 安装包。"
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
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            _append(job, line)
        code = proc.wait()
        with _LOCK:
            job["exitCode"] = code
            job["status"] = "done" if code == 0 else "error"
            if code != 0:
                job["error"] = f"winget 退出码 {code}，请查看日志或改用官网安装包。"
            job["finishedAt"] = int(time.time())
        if code == 0:
            # 安装完成后旧结果已经过期，下次请求重新检测一次。
            invalidate_cache()
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
    data = None if refresh else _load_cache()
    # pandoc 刚下载好但缓存还是“未检测到”时补测一次。
    if data is None or (
        download.get("installed") and not data.get("pandoc", {}).get("found")
    ):
        data = toolchain_status()
        _save_cache(data)
    return {
        **data,
        "install": {
            "running": _CURRENT is not None,
            "jobId": _CURRENT,
        },
        "pandocDownload": download,
    }


@router.post("/pandoc")
def install_pandoc() -> dict[str, Any]:
    """安装包不带 pandoc：首次启动自动下载，这里也给一个手动重试入口。"""
    return pandoc_setup.start()


@router.post("/install")
def install() -> dict[str, Any]:
    global _CURRENT
    with _LOCK:
        if _CURRENT is not None:
            return {"jobId": _CURRENT, "status": "running"}
    if shutil.which("xelatex") or shutil.which("xelatex.exe"):
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
