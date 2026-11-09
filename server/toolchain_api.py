"""写作工具链自检与一键安装（设置页使用）。

安装包里只带 pandoc，PDF 编译需要本机的 TeX 引擎。这里提供两件事：

- ``GET /api/toolchain``：返回 pandoc / TeX 引擎 / bibtex 的检测结果；
- ``POST /api/toolchain/install``：在后台用 winget 安装 MiKTeX，前端轮询进度。

安装是显式操作：不会在启动时自动装，也不会静默执行用户没点过的命令。
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from server.research import pandoc_setup
from server.research.latex import find_winget, toolchain_status

router = APIRouter(prefix="/api/toolchain", tags=["toolchain"])

_MAX_LOG_LINES = 400
_JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
_CURRENT: str | None = None


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
    except Exception as error:  # noqa: BLE001 - 安装失败要回显给用户
        with _LOCK:
            job["status"] = "error"
            job["error"] = str(error)
            job["finishedAt"] = int(time.time())
    finally:
        with _LOCK:
            _CURRENT = None


@router.get("")
def status() -> dict[str, Any]:
    data = toolchain_status()
    data["install"] = {
        "running": _CURRENT is not None,
        "jobId": _CURRENT,
    }
    data["pandocDownload"] = pandoc_setup.status()
    return data


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
