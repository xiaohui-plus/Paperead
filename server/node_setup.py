"""node 运行时的按需安装。

MathJax 完整 TeX 引擎（``cases`` / ``aligned`` / ``bmatrix`` 等环境）要靠
``paperead/vendor/mathjax/tex2svg_server.js`` 执行，而这个脚本需要 node。
node.exe 解压后约 80MB，塞进安装包会顶破 Gitee 单附件上限，所以改为首次
启动在后台下载一个只含 ``node.exe`` 的精简压缩包（约 30MB），解压到
``<数据目录>/engines/node/<version>``，之后离线可用。

整个流程只用 ``urllib`` 读网络、``zipfile`` 解压，不创建任何子进程，
因此不会弹出 cmd / PowerShell 窗口；装好后用隐藏窗口的方式跑一次
``node --version`` 做完整性校验。失败时静默记录状态，公式自动回退
mathtext / 裁图，不影响应用启动。
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

log = logging.getLogger("paperead.node")

NODE_VERSION = "24.11.1"
ASSET_NAME = f"node-{NODE_VERSION}-win-x64-lite.zip"
# 由 scripts/stage-node.ps1 打包后回填；为空时退化为「能运行且版本匹配」校验。
ASSET_SHA256 = "75016D00570E8664406454504F3F1740BC7AFF8A2144B1D2180C3303EBB2DDAB"
CHUNK = 256 * 1024

_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_STATE: dict[str, Any] = {
    "status": "idle",  # idle | running | done | error
    "percent": 0,
    "error": "",
    "startedAt": 0,
    "finishedAt": 0,
}


def install_dir() -> Path:
    """下载的 node 存放位置：``<数据目录>/engines/node/<version>``。"""
    from server.runtime import default_data_dir

    return default_data_dir() / "engines" / "node" / NODE_VERSION


def installed_exe() -> Path | None:
    target = install_dir() / "node.exe"
    return target if target.is_file() else None


def _sources() -> list[str]:
    override = os.environ.get("PAPEREAD_NODE_URL", "").strip()
    if override:
        return [override]
    from paperead import __version__

    return [
        f"https://gitee.com/deng_xiaohui/paperead/releases/download/latest/{ASSET_NAME}",
        f"https://gitee.com/deng_xiaohui/paperead/releases/download/v{__version__}/{ASSET_NAME}",
        f"https://ghproxy.net/https://github.com/xiaohui-plus/Paperead/releases/latest/download/{ASSET_NAME}",
        f"https://github.com/xiaohui-plus/Paperead/releases/latest/download/{ASSET_NAME}",
    ]


def status() -> dict[str, Any]:
    with _LOCK:
        data = dict(_STATE)
    data["installed"] = installed_exe() is not None
    data["version"] = NODE_VERSION
    return data


def _set(**fields: Any) -> None:
    with _LOCK:
        _STATE.update(fields)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest().upper()


def _download(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "paperead"})
    with urllib.request.urlopen(request, timeout=60) as response:
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
                    _set(percent=min(99, int(done * 100 / total)))
    if ASSET_SHA256 and _sha256(target) != ASSET_SHA256:
        target.unlink(missing_ok=True)
        raise ValueError("下载内容校验失败，请重试")


def _extract(archive: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        member = next(
            (name for name in bundle.namelist() if name.lower().endswith("node.exe")),
            None,
        )
        if member is None:
            raise ValueError("压缩包里没有 node.exe")
        with bundle.open(member) as source, target.open("wb") as handle:
            while True:
                block = source.read(CHUNK)
                if not block:
                    break
                handle.write(block)


def _verify(exe: Path) -> None:
    """隐藏窗口跑一次 ``node --version``，确认拿到的确实是可用的 node。"""
    from server.winproc import run_hidden

    try:
        result = run_hidden(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"node 无法运行：{error}") from error
    output = (result.stdout or "").strip()
    if result.returncode != 0 or not output.startswith("v"):
        raise ValueError("下载的 node 无法运行")


def _apply_to_mathjax() -> None:
    """把装好的 node 注册给 MathJax 渲染器，立即生效（不必重启）。"""
    exe = installed_exe()
    if exe is None:
        return
    try:
        from paperead import mathjax_render

        mathjax_render.set_node_path(str(exe))
    except Exception:  # noqa: BLE001 - 注册失败不影响下载结果
        log.warning("注册 node 到 MathJax 失败", exc_info=True)


def _run() -> None:
    work = install_dir()
    work.mkdir(parents=True, exist_ok=True)
    archive = work / ASSET_NAME
    errors: list[str] = []
    for url in _sources():
        try:
            _set(percent=0, error="")
            _download(url, archive)
            _set(percent=95)
            _extract(archive, work / "node.exe")
            _verify(work / "node.exe")
            archive.unlink(missing_ok=True)
            _apply_to_mathjax()
            _set(status="done", percent=100, finishedAt=int(time.time()))
            log.info("node 运行时已安装：%s", work)
            return
        except (
            OSError,
            ValueError,
            zipfile.BadZipFile,
            urllib.error.URLError,
        ) as error:
            host = url.split("/")[2] if "://" in url else url
            errors.append(f"{host}: {error}")
            log.warning("node 运行时下载失败（%s）：%s", host, error)
            archive.unlink(missing_ok=True)
            (work / "node.exe").unlink(missing_ok=True)
    _set(
        status="error",
        error="；".join(errors)[-800:] or "下载失败",
        finishedAt=int(time.time()),
    )


def start() -> dict[str, Any]:
    """开始后台下载；已经装好或正在下载时直接返回当前状态。"""
    global _THREAD
    with _LOCK:
        if installed_exe() is not None:
            _STATE.update(status="done", percent=100)
            return dict(_STATE)
        if _THREAD is not None and _THREAD.is_alive():
            return dict(_STATE)
        _STATE.update(status="running", percent=0, error="", startedAt=int(time.time()))
        _THREAD = threading.Thread(target=_run, daemon=True, name="node-install")
        _THREAD.start()
    return status()


def start_if_missing() -> None:
    """应用启动时调用：没装过就悄悄下载，不阻塞启动。"""
    if os.environ.get("PAPEREAD_MOCK") == "1":
        return
    if os.environ.get("PAPEREAD_NODE_AUTODOWNLOAD", "1") == "0":
        return
    if os.name != "nt":
        # 目前只发布 Windows 精简包；其他平台交给系统 node。
        return
    exe = installed_exe()
    if exe is not None:
        _set(status="done", percent=100)
        _apply_to_mathjax()
        return
    st = status()
    if st["status"] == "running":
        return
    try:
        start()
    except OSError:
        pass