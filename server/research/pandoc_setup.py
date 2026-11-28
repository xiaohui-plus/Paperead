"""pandoc 按需安装。

安装包里不再塞 116MB 的 pandoc：首次启动在后台下载一次，之后离线可用。
下载地址依次尝试 Gitee 发行版、GitHub 发行版（经 ghproxy 镜像）和 GitHub 直连，
任一成功即解压出 ``pandoc.exe`` 放进数据目录 ``tools/pandoc``。

便携版仍然可以自带 pandoc：``server/research/latex.py`` 会优先用安装目录里
``resources/tools/pandoc/pandoc.exe``，找不到才用这里下载的那份。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from server import deps_source

PANDOC_VERSION = "2.19.2"
ASSET_NAME = f"pandoc-{PANDOC_VERSION}-windows-x86_64.zip"
ASSET_SHA256 = "E7A0C92B4AF6CAD31D9899A8B92A3992E18634320349BBF56B729BBBCF71BB99"
CHUNK = 256 * 1024

_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_STATE: dict[str, Any] = {
    "status": "idle",  # idle | running | done | error
    "phase": "",  # download | extract | done
    "percent": 0,
    "bytes": 0,
    "total": 0,
    "source": "",
    "error": "",
    "startedAt": 0,
    "finishedAt": 0,
}


def install_dir() -> Path:
    """下载的 pandoc 存放位置：``<数据目录>/tools/pandoc``。"""
    from server.runtime import default_data_dir

    return default_data_dir() / "tools" / "pandoc"


def installed_exe() -> Path | None:
    target = install_dir() / "pandoc.exe"
    return target if target.is_file() else None


def _sources() -> list[str]:
    override = os.environ.get("PAPEREAD_PANDOC_URL")
    if override:
        return [override]
    from paperead import __version__

    # 依赖仓库优先（固定 tag，不随 App 版本覆盖），主仓库旧地址兜底。
    return deps_source.release_urls(
        ASSET_NAME,
        extra=[
            f"https://gitee.com/deng_xiaohui/paperead/releases/download/v{__version__}/{ASSET_NAME}",
            f"https://ghproxy.net/https://github.com/xiaohui-plus/Paperead/releases/latest/download/{ASSET_NAME}",
            f"https://github.com/xiaohui-plus/Paperead/releases/latest/download/{ASSET_NAME}",
        ],
    )


def status() -> dict[str, Any]:
    with _LOCK:
        data = dict(_STATE)
    data["installed"] = installed_exe() is not None
    data["version"] = PANDOC_VERSION
    alive = _THREAD is not None and _THREAD.is_alive()
    data["threadAlive"] = alive
    if data["status"] == "running" and not alive:
        data["status"] = "error"
        data["error"] = data.get("error") or "下载线程已退出，请重试。"
    return data


def _set(**fields: Any) -> None:
    with _LOCK:
        _STATE.update(fields)


def _download(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "paperead"})
    with urllib.request.urlopen(request, timeout=60) as response:
        # Gitee 空仓库 / 缺少附件会返回 200 + JSON 错误，或 302 到仓库首页 HTML。
        content_type = (response.headers.get("Content-Type") or "").lower()
        if content_type.startswith("application/json"):
            detail = response.read(400).decode("utf-8", errors="replace")
            raise ValueError(f"下载源返回错误：{detail.strip()}")
        if "html" in content_type:
            raise ValueError("下载源返回网页而不是文件（依赖仓库为空或附件不存在）")
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with target.open("wb") as handle:
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                handle.write(block)
                done += len(block)
                _set(phase="download", bytes=done, total=total)
                if total:
                    _set(percent=min(93, int(done * 93 / total)))
    digest = hashlib.sha256(target.read_bytes()).hexdigest().upper()
    if digest != ASSET_SHA256:
        target.unlink(missing_ok=True)
        raise ValueError("下载内容校验失败，请重试")


def _extract(archive: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        member = next(
            (name for name in bundle.namelist() if name.lower().endswith("pandoc.exe")),
            None,
        )
        if member is None:
            raise ValueError("压缩包里没有 pandoc.exe")
        with bundle.open(member) as source, target.open("wb") as handle:
            while True:
                block = source.read(CHUNK)
                if not block:
                    break
                handle.write(block)


def _run() -> None:
    work = install_dir()
    work.mkdir(parents=True, exist_ok=True)
    archive = work / ASSET_NAME
    errors: list[str] = []
    for url in _sources():
        host = url.split("/")[2] if "://" in url else url
        try:
            _set(phase="download", percent=0, bytes=0, total=0, source=host, error="")
            _download(url, archive)
            _set(phase="extract", percent=95)
            _extract(archive, work / "pandoc.exe")
            archive.unlink(missing_ok=True)
            _set(status="done", phase="done", percent=100, finishedAt=int(time.time()))
            return
        except (OSError, ValueError, zipfile.BadZipFile, urllib.error.URLError) as error:
            errors.append(f"{host}: {error}")
            archive.unlink(missing_ok=True)
            _set(phase="download", source=host, error=f"{host}：{error}，正在尝试备用下载源…")
    _set(
        status="error",
        phase="error",
        error="；".join(errors)[-800:] or "下载失败",
        finishedAt=int(time.time()),
    )


def start() -> dict[str, Any]:
    """开始后台下载；已经装好或正在下载时直接返回当前状态。"""
    global _THREAD
    with _LOCK:
        if installed_exe() is not None:
            _STATE.update(status="done", phase="done", percent=100)
            return dict(_STATE)
        if _THREAD is not None and _THREAD.is_alive():
            return dict(_STATE)
        if _STATE.get("status") == "running":
            _STATE.update(status="error", phase="error", error="下载线程已退出，请重试。")
        _STATE.update(
            status="running",
            phase="download",
            percent=0,
            bytes=0,
            total=0,
            source="",
            error="",
            startedAt=int(time.time()),
            finishedAt=0,
        )
        _THREAD = threading.Thread(target=_run, daemon=True, name="pandoc-install")
        _THREAD.start()
    return status()


def start_if_missing() -> None:
    """应用启动时调用：没装过就悄悄下载，不阻塞启动。

    安装包内置/系统 PATH 里已有 pandoc 时直接标记完成，不再多下 24MB。
    """
    if installed_exe() is not None:
        return
    try:
        from server.research.latex import find_pandoc

        if find_pandoc():
            with _LOCK:
                _STATE.update(status="done", phase="done", percent=100)
            return
    except Exception:  # noqa: BLE001 - 探测失败时按缺失处理
        pass
    st = status()
    if st["status"] == "running":
        return
    try:
        start()
    except OSError:
        pass
